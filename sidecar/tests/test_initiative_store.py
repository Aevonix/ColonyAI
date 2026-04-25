"""Tests for InitiativeStore and initiative lifecycle."""

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from colony_sidecar.initiatives.store import InitiativeStore
from colony_sidecar.initiatives.models import InitiativeStatus, StoredInitiative


class TestInitiativeStore:
    """Tests for InitiativeStore CRUD operations."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> InitiativeStore:
        """Create a fresh InitiativeStore for each test."""
        return InitiativeStore(state_dir=tmp_path)
        return InitiativeStore(state_dir=tmp_path)

    def test_create_initiative(self, store: InitiativeStore) -> None:
        """Test creating an initiative."""
        initiative = store.create(
            type="notification",
            description="Test notification",
            priority=0.8,
            timeout_seconds=300,
        )

        assert initiative is not None
        assert initiative.id.startswith("init-")
        assert initiative.type == "notification"
        assert initiative.description == "Test notification"
        assert initiative.priority == 0.8
        assert initiative.status == "pending"

    def test_get_initiative(self, store: InitiativeStore) -> None:
        """Test retrieving an initiative."""
        created = store.create(
            type="task",
            description="Test task",
        )

        initiative = store.get(created.id)
        assert initiative is not None
        assert initiative.id == created.id

        # Non-existent
        assert store.get("nonexistent") is None

    def test_assign_initiative(self, store: InitiativeStore) -> None:
        """Test assigning an initiative to an agent."""
        initiative = store.create(
            type="notification",
            description="Test",
        )

        assigned = store.assign(initiative.id, "agent-1")
        assert assigned is not None
        assert assigned.status == "assigned"
        assert assigned.assigned_agent_id == "agent-1"
        assert assigned.assigned_at is not None

    def test_acknowledge_initiative(self, store: InitiativeStore) -> None:
        """Test acknowledging an initiative."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")

        acked = store.acknowledge(initiative.id, "agent-1")
        assert acked is not None
        assert acked.status == "acknowledged"
        assert acked.acknowledged_at is not None

    def test_complete_initiative(self, store: InitiativeStore) -> None:
        """Test completing an initiative."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")
        store.acknowledge(initiative.id, "agent-1")

        completed = store.complete(
            initiative.id,
            "agent-1",
            result="Task completed successfully",
            result_metadata={"duration_ms": 1500},
        )
        assert completed is not None
        assert completed.status == "completed"
        assert completed.result == "Task completed successfully"
        assert completed.completed_at is not None

    def test_fail_initiative(self, store: InitiativeStore) -> None:
        """Test failing an initiative."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")

        failed = store.fail(
            initiative.id,
            "agent-1",
            reason="Agent disconnected",
            retry=True,
        )
        assert failed is not None
        assert failed.status == "failed"
        assert failed.failed_reason == "Agent disconnected"
        assert failed.attempt_count == 1

    def test_cancel_initiative(self, store: InitiativeStore) -> None:
        """Test cancelling an initiative."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")

        cancelled = store.cancel(initiative.id, "user-1", reason="User cancelled")
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.cancelled_by == "user-1"
        assert cancelled.cancelled_reason == "User cancelled"

    def test_retry_failed_initiative(self, store: InitiativeStore) -> None:
        """Test retrying a failed initiative."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")
        store.fail(initiative.id, "agent-1", reason="Failed")

        # Retry should reset to pending
        retried = store.retry(initiative.id)
        assert retried is not None
        assert retried.status == "pending"
        assert retried.assigned_agent_id is None
        assert retried.attempt_count == 1  # Retained for tracking

    def test_deduplication(self, store: InitiativeStore) -> None:
        """Test initiative deduplication."""
        # Create with dedup_key
        first = store.create(
            type="notification",
            description="First",
            dedup_key="unique-key-1",
        )
        assert first is not None

        # Same dedup_key should return existing
        second = store.create(
            type="notification",
            description="Second",
            dedup_key="unique-key-1",
        )
        assert second.id == first.id

        # Different dedup_key creates new
        third = store.create(
            type="notification",
            description="Third",
            dedup_key="unique-key-2",
        )
        assert third.id != first.id

    def test_list_initiatives(self, store: InitiativeStore) -> None:
        """Test listing initiatives with filters."""
        # Create multiple initiatives
        store.create(type="notification", description="1", priority=0.9)
        store.create(type="task", description="2", priority=0.5)
        i3 = store.create(type="notification", description="3", priority=0.7)
        store.assign(i3.id, "agent-1")
        i4 = store.create(type="task", description="4", priority=0.3)
        store.assign(i4.id, "agent-2")

        # List all
        all_initiatives = store.list()
        assert len(all_initiatives) == 4

        # Filter by status
        pending = store.list(status="pending")
        assert len(pending) == 2

        assigned = store.list(status="assigned")
        assert len(assigned) == 2

        # Filter by agent
        agent1 = store.list(assigned_agent_id="agent-1")
        assert len(agent1) == 1

    def test_expiry(self, store: InitiativeStore) -> None:
        """Test initiative expiry."""
        # Create already-expired initiative
        initiative = store.create(
            type="notification",
            description="Test",
            timeout_seconds=1,  # 1 second timeout
        )

        # Manually set created_at to past
        store.update(
            initiative.id,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        # Check expiry
        expired = store.get(initiative.id)
        assert expired is not None
        assert expired.is_expired is True

        # Complete should mark as failed due to expiry
        result = store.complete(initiative.id, "agent-1", result="Too late")
        assert result is not None
        assert result.status == "failed"
        assert result.failed_reason == "initiative_expired"

    def test_history_logging(self, store: InitiativeStore) -> None:
        """Test initiative history tracking."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")
        store.acknowledge(initiative.id, "agent-1")
        store.complete(initiative.id, "agent-1", result="Done")

        history = store.get_history(initiative.id)
        assert len(history) == 4  # created, assigned, acknowledged, completed
        
        actions = [h["action"] for h in history]
        assert "created" in actions
        assert "assigned" in actions
        assert "acknowledged" in actions
        assert "completed" in actions

    def test_dead_letter_queue(self, store: InitiativeStore) -> None:
        """Test dead letter queue for failed initiatives."""
        # Create and fail multiple times to exceed max attempts
        initiative = store.create(
            type="notification",
            description="Test",
            max_attempts=3,
        )
        
        for _ in range(3):
            store.assign(initiative.id, f"agent-{_}")
            store.fail(initiative.id, f"agent-{_}", reason="Failed")

        # Should be in DLQ now
        dlq = store.get_dead_letter_queue()
        assert len(dlq) == 1
        assert dlq[0].id == initiative.id

        # Can recover from DLQ
        recovered = store.recover_from_dlq(initiative.id)
        assert recovered is not None
        assert recovered.status == "pending"
        assert recovered.attempt_count == 0  # Reset


class TestStoredInitiative:
    """Tests for StoredInitiative model methods."""

    def test_is_active(self) -> None:
        """Test is_active property."""
        initiative = StoredInitiative(
            id="init-1",
            type="notification",
            description="Test",
            priority=0.5,
            rationale="Test rationale",
        )
        assert initiative.is_active is True

        initiative.status = "completed"
        assert initiative.is_active is False

    def test_can_assign(self) -> None:
        """Test can_assign property."""
        initiative = StoredInitiative(
            id="init-1",
            type="notification",
            description="Test",
            priority=0.5,
            rationale="Test rationale",
            status="pending",
        )
        assert initiative.can_assign is True

        initiative.status = "assigned"
        assert initiative.can_assign is False

    def test_is_expired(self) -> None:
        """Test is_expired property."""
        # Not expired
        initiative = StoredInitiative(
            id="init-1",
            type="notification",
            description="Test",
            priority=0.5,
            rationale="Test rationale",
            created_at=datetime.now(timezone.utc),
            timeout_seconds=3600,
        )
        assert initiative.is_expired is False

        # Expired
        initiative.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        initiative.timeout_seconds = 3600  # 1 hour
        assert initiative.is_expired is True

        # No timeout = never expires
        initiative.timeout_seconds = None
        assert initiative.is_expired is False


class TestInitiativePriority:
    """Tests for initiative priority handling."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> InitiativeStore:
        """Create a fresh InitiativeStore for each test."""
        return InitiativeStore(state_dir=tmp_path)
        return InitiativeStore(state_dir=tmp_path)

    def test_priority_sorting(self, store: InitiativeStore) -> None:
        """Test that initiatives are sorted by priority."""
        store.create(type="notification", description="Low", priority=0.3)
        store.create(type="notification", description="High", priority=0.9)
        store.create(type="notification", description="Medium", priority=0.6)

        initiatives = store.list()
        assert len(initiatives) == 3
        # Should be sorted by priority descending
        assert initiatives[0].priority == 0.9
        assert initiatives[1].priority == 0.6
        assert initiatives[2].priority == 0.3

    def test_update_priority(self, store: InitiativeStore) -> None:
        """Test updating initiative priority."""
        initiative = store.create(
            type="notification",
            description="Test",
            priority=0.5,
        )

        updated = store.update(initiative.id, priority=0.95)
        assert updated is not None
        assert updated.priority == 0.95


class TestInitiativeTimeout:
    """Tests for initiative timeout handling."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> InitiativeStore:
        """Create a fresh InitiativeStore for each test."""
        return InitiativeStore(state_dir=tmp_path)
        return InitiativeStore(state_dir=tmp_path)

    def test_get_timed_out(self, store: InitiativeStore) -> None:
        """Test retrieving timed-out initiatives."""
        # Create initiative that should timeout
        initiative = store.create(
            type="notification",
            description="Test",
            timeout_seconds=60,
        )
        
        # Set assigned_at to past
        store.update(
            initiative.id,
            status="acknowledged",
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        timed_out = store.get_timed_out(timeout_seconds=60)
        assert len(timed_out) == 1
        assert timed_out[0].id == initiative.id

    def test_get_stale_assigned(self, store: InitiativeStore) -> None:
        """Test retrieving stale assigned initiatives."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")
        
        # Set assigned_at to past
        store.update(
            initiative.id,
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )

        stale = store.get_stale_assigned(stale_hours=1)
        assert len(stale) == 1
