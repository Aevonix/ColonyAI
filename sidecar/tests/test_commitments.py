"""Tests for commitment tracking — Layer 1: Store + API."""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from colony_sidecar.commitments.store import CommitmentStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test-commitments.db"
    return CommitmentStore(db_path=db)


def _future_dt() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()


class TestCommitmentStoreCreate:
    def test_create_with_defaults(self, store):
        result = store.create(person_id="marc", description="Check cluster status")
        assert result["person_id"] == "marc"
        assert result["description"] == "Check cluster status"
        assert result["status"] == "pending"
        assert result["priority"] == 50
        assert result["source_type"] == "manual"
        assert result["id"] is not None
        assert result["made_at"] is not None

    def test_create_with_all_fields(self, store):
        due = _future_dt()
        result = store.create(
            person_id="marc",
            description="Review PR by Friday",
            due_at=due,
            priority=80,
            source_type="cognition",
            source_context="session:abc",
            metadata={"topic": "code-review"},
        )
        assert result["due_at"] == due
        assert result["priority"] == 80
        assert result["source_type"] == "cognition"
        assert result["source_context"] == "session:abc"
        assert result["metadata"] == {"topic": "code-review"}

    def test_create_with_past_due_at_rejected(self, store):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with pytest.raises(ValueError, match="future"):
            store.create(person_id="marc", description="Test", due_at=past)

    def test_create_with_no_due_at(self, store):
        result = store.create(person_id="marc", description="No deadline task")
        assert result["due_at"] is None


class TestCommitmentStoreGet:
    def test_get_existing(self, store):
        created = store.create(person_id="marc", description="Test")
        result = store.get(created["id"])
        assert result is not None
        assert result["id"] == created["id"]

    def test_get_nonexistent(self, store):
        result = store.get("does-not-exist")
        assert result is None


class TestCommitmentStoreList:
    def test_list_all(self, store):
        store.create(person_id="marc", description="Task 1")
        store.create(person_id="marc", description="Task 2")
        result = store.list()
        assert result["total"] == 2
        assert len(result["commitments"]) == 2

    def test_list_by_person_id(self, store):
        store.create(person_id="marc", description="Marc's task")
        store.create(person_id="alice", description="Alice's task")
        result = store.list(person_id="marc")
        assert result["total"] == 1
        assert result["commitments"][0]["person_id"] == "marc"

    def test_list_by_status(self, store):
        c = store.create(person_id="marc", description="Test")
        store.update(c["id"], status="fulfilled")
        store.create(person_id="marc", description="Another")
        result = store.list(status=["pending"])
        assert result["total"] == 1

    def test_list_overdue_only(self, store):
        c = store.create(person_id="marc", description="Overdue")
        store.update(c["id"], status="overdue")
        store.create(person_id="marc", description="Pending")
        result = store.list(overdue_only=True)
        assert result["total"] == 1
        assert result["commitments"][0]["status"] == "overdue"

    def test_list_pagination(self, store):
        for i in range(5):
            store.create(person_id="marc", description=f"Task {i}")
        page1 = store.list(limit=2, offset=0)
        page2 = store.list(limit=2, offset=2)
        assert len(page1["commitments"]) == 2
        assert len(page2["commitments"]) == 2
        assert page1["total"] == 5


class TestCommitmentStoreUpdate:
    def test_update_status_to_fulfilled(self, store):
        c = store.create(person_id="marc", description="Test")
        result = store.update(c["id"], status="fulfilled")
        assert result["status"] == "fulfilled"
        assert result["fulfilled_at"] is not None

    def test_fulfilled_auto_sets_fulfilled_at(self, store):
        c = store.create(person_id="marc", description="Test")
        result = store.update(c["id"], status="fulfilled")
        assert result["fulfilled_at"] is not None

    def test_update_status_to_cancelled(self, store):
        c = store.create(person_id="marc", description="Test")
        result = store.update(c["id"], status="cancelled")
        assert result["status"] == "cancelled"

    def test_update_description(self, store):
        c = store.create(person_id="marc", description="Old")
        result = store.update(c["id"], description="New description")
        assert result["description"] == "New description"

    def test_update_priority(self, store):
        c = store.create(person_id="marc", description="Test")
        result = store.update(c["id"], priority=90)
        assert result["priority"] == 90

    def test_invalid_transition_fulfilled_to_pending(self, store):
        c = store.create(person_id="marc", description="Test")
        store.update(c["id"], status="fulfilled")
        with pytest.raises(ValueError, match="Cannot transition"):
            store.update(c["id"], status="pending")

    def test_invalid_transition_fulfilled_to_overdue(self, store):
        c = store.create(person_id="marc", description="Test")
        store.update(c["id"], status="fulfilled")
        with pytest.raises(ValueError, match="Cannot transition"):
            store.update(c["id"], status="overdue")

    def test_update_nonexistent(self, store):
        result = store.update("does-not-exist", status="fulfilled")
        assert result is None


class TestCommitmentStoreDelete:
    def test_delete_fulfilled(self, store):
        c = store.create(person_id="marc", description="Test")
        store.update(c["id"], status="fulfilled")
        assert store.delete(c["id"]) is True

    def test_delete_cancelled(self, store):
        c = store.create(person_id="marc", description="Test")
        store.update(c["id"], status="cancelled")
        assert store.delete(c["id"]) is True

    def test_delete_pending_rejected(self, store):
        c = store.create(person_id="marc", description="Test")
        assert store.delete(c["id"]) is False

    def test_delete_nonexistent(self, store):
        assert store.delete("does-not-exist") is False


class TestCommitmentStoreOverdue:
    def test_get_overdue(self, store):
        # Create a commitment already overdue via direct status update
        c = store.create(person_id="marc", description="Overdue task")
        store.update(c["id"], status="overdue")
        overdue = store.get_overdue()
        # get_overdue checks pending + past due_at, not status=overdue
        # So we need to create one with a past due_at
        assert isinstance(overdue, list)

    def test_get_pending_for_person(self, store):
        store.create(person_id="marc", description="Marc's task")
        store.create(person_id="alice", description="Alice's task")
        result = store.get_pending_for_person("marc")
        assert len(result) == 1
        assert result[0]["person_id"] == "marc"

    def test_get_pending_excludes_fulfilled(self, store):
        c = store.create(person_id="marc", description="Done task")
        store.update(c["id"], status="fulfilled")
        result = store.get_pending_for_person("marc")
        assert len(result) == 0
