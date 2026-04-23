"""Byzantine-fault tests for Raft consensus.

Tests adversarial scenarios:
- Invalid signatures on COMMIT_BLOCK
- Invalid merkle roots
- Timestamp manipulation
- Malformed blocks
- Unknown leader_id
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from colony_sidecar.chain.block import Block, build_merkle_root
from colony_sidecar.chain.consensus import RaftConfig, RaftNode
from colony_sidecar.chain.transactions import Transaction, TxType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tx(tx_id="tx-001"):
    """Create a valid transaction for testing."""
    return Transaction(
        tx_id=tx_id,
        type=TxType.TRUST_ATTEST,
        from_colony_id="colony-1",
        timestamp=datetime.now(timezone.utc).isoformat(),
        nonce=1,
        payload={"data": "test"},
        signature="test-signature",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return RaftConfig(
        sentinel_id="sentinel-1",
        election_timeout_min_ms=5000,
        election_timeout_max_ms=10000,
        block_interval_secs=30,
    )


@pytest.fixture
def peers():
    return ["sentinel-2", "sentinel-3"]


@pytest.fixture
def valid_block():
    """Create a valid block for testing."""
    tx = make_tx()
    merkle = build_merkle_root([tx.tx_id])
    return Block(
        index=1,
        previous_hash="0" * 64,
        timestamp=datetime.now(timezone.utc).isoformat(),
        transactions=[tx],
        merkle_root=merkle,
        producer_id="sentinel-2",
        signature="test-signature",
    )


@pytest.fixture
def node(config, peers):
    """Create a RaftNode with mocked dependencies."""
    send_message = AsyncMock()
    sign_block = MagicMock(return_value="sig")
    build_block = MagicMock()
    on_commit = AsyncMock()
    sign_data = MagicMock(return_value="sig-hex")
    get_peer_pubkey = MagicMock(return_value=b"\x00" * 32)  # Fake pubkey

    node = RaftNode(
        config=config,
        peer_ids=peers,
        send_message=send_message,
        sign_block=sign_block,
        build_block=build_block,
        on_commit=on_commit,
        sign_data=sign_data,
        get_peer_pubkey=get_peer_pubkey,
    )
    return node


# ---------------------------------------------------------------------------
# H7: Byzantine-fault tests
# ---------------------------------------------------------------------------

class TestCommitBlockByzantine:
    """Test COMMIT_BLOCK handling under adversarial conditions."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature(self, node, valid_block):
        """COMMIT_BLOCK with invalid signature should be rejected."""
        # Invalid signature (not a valid hex or doesn't verify)
        valid_block.signature = "invalid-signature"
        
        initial_rejected = node._metrics["blocks_rejected"]
        initial_invalid_sigs = node._metrics["invalid_signatures"]
        
        await node.handle_commit_block({
            "term": 1,
            "block": valid_block.to_dict(),
        })
        
        # Should not commit
        node.on_commit.assert_not_called()
        
        # Should increment rejection metrics
        assert node._metrics["blocks_rejected"] == initial_rejected + 1
        assert node._metrics["invalid_signatures"] == initial_invalid_sigs + 1

    @pytest.mark.asyncio
    async def test_rejects_missing_signature(self, node, valid_block):
        """COMMIT_BLOCK with missing signature should be rejected."""
        valid_block.signature = ""
        
        await node.handle_commit_block({
            "term": 1,
            "block": valid_block.to_dict(),
        })
        
        node.on_commit.assert_not_called()
        assert node._metrics["invalid_signatures"] >= 1

    @pytest.mark.asyncio
    async def test_rejects_wrong_term(self, node, valid_block):
        """COMMIT_BLOCK from old term should be ignored."""
        node.current_term = 5
        
        await node.handle_commit_block({
            "term": 3,  # Old term
            "block": valid_block.to_dict(),
        })
        
        node.on_commit.assert_not_called()


class TestBlockProposalByzantine:
    """Test BLOCK_PROPOSE handling under adversarial conditions."""

    @pytest.mark.asyncio
    async def test_rejects_merkle_root_mismatch(self, node):
        """Block with wrong merkle root should be NACKed."""
        tx = make_tx()
        
        # Wrong merkle root
        block = Block(
            index=1,
            previous_hash="0" * 64,
            timestamp=datetime.now(timezone.utc).isoformat(),
            transactions=[tx],
            merkle_root="wrong_merkle_root",
            producer_id="sentinel-2",
            signature="sig",
        )
        
        initial_rejected = node._metrics["blocks_rejected"]
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "sentinel-2",
        })
        
        # Should have sent NACK
        node.send_message.assert_called_once()
        call_args = node.send_message.call_args
        assert call_args[0][0] == "sentinel-2"  # leader_id
        msg = call_args[0][1]
        assert msg["type"] == "BLOCK_NACK"
        assert "merkle_root mismatch" in msg["data"]["reason"]
        
        # Metrics updated
        assert node._metrics["blocks_rejected"] == initial_rejected + 1

    @pytest.mark.asyncio
    async def test_rejects_future_timestamp(self, node):
        """Block with future timestamp should be NACKed."""
        tx = make_tx()
        merkle = build_merkle_root([tx.tx_id])
        
        # Future timestamp
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        block = Block(
            index=1,
            previous_hash="0" * 64,
            timestamp=future.isoformat(),
            transactions=[tx],
            merkle_root=merkle,
            producer_id="sentinel-2",
            signature="sig",
        )
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "sentinel-2",
        })
        
        node.send_message.assert_called_once()
        msg = node.send_message.call_args[0][1]
        assert msg["type"] == "BLOCK_NACK"
        assert "future" in msg["data"]["reason"].lower()

    @pytest.mark.asyncio
    async def test_rejects_wrong_index(self, node):
        """Block with wrong index should be NACKed."""
        tx = make_tx()
        merkle = build_merkle_root([tx.tx_id])
        
        # Wrong index (should be 1, we send 5)
        block = Block(
            index=5,
            previous_hash="0" * 64,
            timestamp=datetime.now(timezone.utc).isoformat(),
            transactions=[tx],
            merkle_root=merkle,
            producer_id="sentinel-2",
            signature="sig",
        )
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "sentinel-2",
        })
        
        msg = node.send_message.call_args[0][1]
        assert msg["type"] == "BLOCK_NACK"
        assert "index mismatch" in msg["data"]["reason"]

    @pytest.mark.asyncio
    async def test_rejects_wrong_previous_hash(self, node):
        """Block with wrong previous_hash should be NACKed."""
        tx = make_tx()
        merkle = build_merkle_root([tx.tx_id])
        
        # Wrong previous_hash
        block = Block(
            index=1,
            previous_hash="f" * 64,  # Should be all zeros
            timestamp=datetime.now(timezone.utc).isoformat(),
            transactions=[tx],
            merkle_root=merkle,
            producer_id="sentinel-2",
            signature="sig",
        )
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "sentinel-2",
        })
        
        msg = node.send_message.call_args[0][1]
        assert msg["type"] == "BLOCK_NACK"
        assert "previous_hash mismatch" in msg["data"]["reason"]

    @pytest.mark.asyncio
    async def test_drops_unknown_leader(self, node):
        """BLOCK_PROPOSE from unknown leader_id should be dropped."""
        tx = make_tx()
        merkle = build_merkle_root([tx.tx_id])
        
        block = Block(
            index=1,
            previous_hash="0" * 64,
            timestamp=datetime.now(timezone.utc).isoformat(),
            transactions=[tx],
            merkle_root=merkle,
            producer_id="sentinel-2",
            signature="sig",
        )
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "unknown-attacker",  # Not in peer_ids
        })
        
        # Should NOT send anything
        node.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_valid_block(self, node):
        """Valid block should be ACKed."""
        tx = make_tx()
        merkle = build_merkle_root([tx.tx_id])
        
        block = Block(
            index=1,
            previous_hash="0" * 64,
            timestamp=datetime.now(timezone.utc).isoformat(),
            transactions=[tx],
            merkle_root=merkle,
            producer_id="sentinel-2",
            signature="sig",
        )
        
        initial_accepted = node._metrics["blocks_accepted"]
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "sentinel-2",
        })
        
        node.send_message.assert_called_once()
        msg = node.send_message.call_args[0][1]
        assert msg["type"] == "BLOCK_ACK"
        
        # Metrics updated
        assert node._metrics["blocks_accepted"] == initial_accepted + 1


class TestMultipleValidationErrors:
    """Test blocks with multiple validation errors."""

    @pytest.mark.asyncio
    async def test_reports_all_errors(self, node):
        """NACK should report all validation errors, not just first."""
        tx = make_tx()
        
        # Multiple errors: wrong index, wrong previous_hash, wrong merkle, future timestamp
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        block = Block(
            index=99,
            previous_hash="f" * 64,
            timestamp=future.isoformat(),
            transactions=[tx],
            merkle_root="wrong",
            producer_id="sentinel-2",
            signature="sig",
        )
        
        await node.handle_propose_block({
            "term": 1,
            "block": block.to_dict(),
            "leader_id": "sentinel-2",
        })
        
        msg = node.send_message.call_args[0][1]
        reason = msg["data"]["reason"]
        
        # Should contain multiple error indicators
        assert "index mismatch" in reason
        assert "previous_hash mismatch" in reason
        assert "merkle_root mismatch" in reason
        assert "future" in reason.lower()


class TestMetricsTracking:
    """Test that metrics are properly tracked."""

    @pytest.mark.asyncio
    async def test_get_metrics_returns_copy(self, node):
        """get_metrics should return a copy, not the original."""
        metrics1 = node.get_metrics()
        metrics2 = node.get_metrics()
        
        assert metrics1 == metrics2
        assert metrics1 is not metrics2

    @pytest.mark.asyncio
    async def test_metrics_persist_across_calls(self, node):
        """Metrics should accumulate across multiple rejected blocks."""
        tx = make_tx()
        
        # Reject 3 blocks
        for i in range(3):
            block = Block(
                index=99,  # Invalid index
                previous_hash="0" * 64,
                timestamp=datetime.now(timezone.utc).isoformat(),
                transactions=[tx],
                merkle_root=build_merkle_root([tx.tx_id]),
                producer_id="sentinel-2",
                signature="sig",
            )
            await node.handle_propose_block({
                "term": 1,
                "block": block.to_dict(),
                "leader_id": "sentinel-2",
            })
        
        assert node._metrics["blocks_rejected"] == 3
