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

    def test_create_initiative(self, store: InitiativeStore) -> None:
        """Test creating an initiative."""
        initiative = store.create(
            type="notification",
            description="Test notification",
            priority=0.8,
            timeout_seconds=300,
        )

        assert initiative is not None
        assert initiative.id.startswith("init-") is False  # UUID format, not "init-" prefix
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

        assigned = store.assign(initiative.id, "agent-1", "Test Agent")
        assert assigned is not None
        assert assigned.status == "assigned"
        assert assigned.assigned_agent_id == "agent-1"
        assert assigned.assigned_at is not None

    def test_acknowledge_initiative(self, store: InitiativeStore) -> None:
        """Test acknowledging an initiative."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1")

        # acknowledge requires agent_id to match
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
            retry=False,
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
        store.fail(initiative.id, "agent-1", reason="Failed", retry=True)

        # Retry=True resets to pending automatically
        # The fail() method handles retry internally
        retried = store.get(initiative.id)
        assert retried is not None
        assert retried.status == "pending"
        assert retried.attempt_count == 1

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

        # Filter by status (list takes list of statuses)
        pending = store.list(status=["pending"])
        assert len(pending) == 2

        assigned = store.list(status=["assigned"])
        assert len(assigned) == 2

    def test_expiry(self, store: InitiativeStore) -> None:
        """Test initiative expiry."""
        # Create initiative that expires soon
        initiative = store.create(
            type="notification",
            description="Test",
            timeout_seconds=1,  # 1 second timeout
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        # Check not expired yet
        fresh = store.get(initiative.id)
        assert fresh is not None
        assert fresh.is_expired is False

    def test_history_logging(self, store: InitiativeStore) -> None:
        """Test initiative history tracking."""
        initiative = store.create(type="notification", description="Test")
        store.assign(initiative.id, "agent-1", "Agent 1")
        store.acknowledge(initiative.id, "agent-1")
        store.complete(initiative.id, "agent-1", result="Done")

        history = store.get_history(initiative.id)
        assert len(history) >= 3  # assigned, acknowledged, completed
        
        actions = [h.action for h in history]
        assert "assigned" in actions
        assert "acknowledged" in actions
        assert "completed" in actions


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

    def test_can_retry(self) -> None:
        """Test can_retry property."""
        initiative = StoredInitiative(
            id="init-1",
            type="notification",
            description="Test",
            priority=0.5,
            rationale="Test rationale",
            status="failed",
            attempt_count=1,
            max_attempts=3,
        )
        assert initiative.can_retry is True

        # Max attempts reached
        initiative.attempt_count = 3
        assert initiative.can_retry is False

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
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert initiative.is_expired is False

        # Expired
        initiative.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        assert initiative.is_expired is True

        # No expiry = never expires
        initiative.expires_at = None
        assert initiative.is_expired is False

    def test_is_timed_out(self) -> None:
        """Test is_timed_out property."""
        # Not timed out
        initiative = StoredInitiative(
            id="init-1",
            type="notification",
            description="Test",
            priority=0.5,
            rationale="Test rationale",
            status="assigned",
            assigned_at=datetime.now(timezone.utc),
            timeout_seconds=3600,
        )
        assert initiative.is_timed_out is False

        # Timed out
        initiative.assigned_at = datetime.now(timezone.utc) - timedelta(hours=2)
        initiative.timeout_seconds = 3600  # 1 hour
        assert initiative.is_timed_out is True


class TestInitiativePriority:
    """Tests for initiative priority handling."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> InitiativeStore:
        """Create a fresh InitiativeStore for each test."""
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


class TestDedupOutcomes:
    """create_with_outcome: explicit outcomes + time-bucketed re-arm + cross-period guard."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> InitiativeStore:
        return InitiativeStore(state_dir=tmp_path)

    def test_fresh_create_reports_created(self, store: InitiativeStore) -> None:
        init, outcome = store.create_with_outcome(type="task", description="x", dedup_key="task:1")
        assert outcome == "created" and init.is_active

    def test_active_dup_is_deduped_active(self, store: InitiativeStore) -> None:
        a, _ = store.create_with_outcome(type="task", description="x", dedup_key="task:1")
        b, outcome = store.create_with_outcome(type="task", description="x", dedup_key="task:1")
        assert outcome == "deduped_active" and b.id == a.id

    def test_completed_same_period_is_deduped_terminal(self, store: InitiativeStore) -> None:
        a, _ = store.create_with_outcome(type="task", description="x", dedup_key="task:1")
        store.complete(a.id, agent_id="agent", result="done")
        _, outcome = store.create_with_outcome(type="task", description="x", dedup_key="task:1")
        assert outcome == "deduped_terminal"

    def test_failed_reactivates(self, store: InitiativeStore) -> None:
        a, _ = store.create_with_outcome(type="deliver", description="x", dedup_key="deliver:1")
        store.fail(a.id, agent_id="agent", reason="boom")
        b, outcome = store.create_with_outcome(type="deliver", description="x", dedup_key="deliver:1")
        assert outcome == "reactivated" and b.is_active

    def test_next_period_re_arms_after_completion(self, store: InitiativeStore) -> None:
        a, _ = store.create_with_outcome(type="relationship", description="x",
                                         dedup_key="rel:1:wk1", dedup_base="rel:1")
        store.complete(a.id, agent_id="agent", result="done")
        # new period key, base no longer active -> fresh instance
        _, outcome = store.create_with_outcome(type="relationship", description="x",
                                               dedup_key="rel:1:wk2", dedup_base="rel:1")
        assert outcome == "created"

    def test_cross_period_active_is_suppressed(self, store: InitiativeStore) -> None:
        # wk1 instance still ACTIVE when wk2 comes around -> no duplicate
        store.create_with_outcome(type="relationship", description="x",
                                  dedup_key="rel:1:wk1", dedup_base="rel:1")
        b, outcome = store.create_with_outcome(type="relationship", description="x",
                                               dedup_key="rel:1:wk2", dedup_base="rel:1")
        assert outcome == "deduped_active"

    def test_create_shim_returns_initiative(self, store: InitiativeStore) -> None:
        init = store.create(type="task", description="x", dedup_key="task:9")
        assert isinstance(init, StoredInitiative)
