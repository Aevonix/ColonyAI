"""Durable host-event -> scoped concern reduction (Mind P2)."""

from __future__ import annotations

from typing import Any

import pytest

from colony_sidecar.self_model.event_concerns import EventConcernReducer
from colony_sidecar.self_model.workspace import ConcernStore
from colony_sidecar.api.authority import required_scope


class FakeJournal:
    def __init__(self, events=None, *, first_available=None):
        self.events = list(events or [])
        self.first_available = first_available

    def current(self):
        return max((int(event["seq"]) for event in self.events), default=0)

    def replay(self, *, after_seq=0, limit=500, **_kwargs):
        first = (
            int(self.first_available)
            if self.first_available is not None
            else min((int(event["seq"]) for event in self.events), default=0)
        )
        selected = [event for event in self.events if int(event["seq"]) > int(after_seq)]
        return {
            "events": selected[:limit],
            "lastSeq": int(selected[min(len(selected), limit) - 1]["seq"])
            if selected else 0,
            "hasMore": len(selected) > limit,
            "firstAvailableSeq": first,
            "journalLastSeq": self.current(),
            "corruptCount": 0,
        }


def event(seq: int, kind: str, data: Any, *, event_id: str | None = None):
    return {
        "seq": seq,
        "ulid": event_id or f"event-{seq}",
        "type": kind,
        "occurredAt": f"2026-07-12T08:{seq:02d}:00+00:00",
        "recordedAt": f"2026-07-12T08:{seq:02d}:01+00:00",
        "data": data,
    }


@pytest.fixture(autouse=True)
def reducer_env(monkeypatch):
    monkeypatch.setenv("COLONY_EVENT_CONCERNS", "shadow")
    monkeypatch.setenv("COLONY_EVENT_CONCERNS_BOOTSTRAP", "replay")
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    monkeypatch.delenv("COLONY_EVENT_CONCERNS_GAP_POLICY", raising=False)


def make(tmp_path, journal: FakeJournal):
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = EventConcernReducer(
        store,
        replay_fn=journal.replay,
        current_sequence_fn=journal.current,
    )
    return store, reducer


def test_tail_bootstrap_does_not_invent_historical_concerns(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_EVENT_CONCERNS_BOOTSTRAP", "tail")
    journal = FakeJournal([
        event(1, "commitment.overdue", {
            "commitment_id": "cm-old", "person_id": "person-owner",
            "description": "old state",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    first = reducer.run_once()
    assert first["bootstrapped"] and first["cursor"] == 1
    assert store.active() == []

    journal.events.append(event(2, "commitment.overdue", {
        "commitment_id": "cm-new", "person_id": "person-owner",
        "description": "new material state",
    }))
    second = reducer.run_once()
    assert second["dispositions"] == {"created": 1}
    assert [concern.summary for concern in store.active()] == ["new material state"]


def test_restart_resumes_exactly_once(tmp_path):
    journal = FakeJournal([
        event(1, "commitment.overdue", {
            "commitment_id": "cm-1", "person_id": "person-owner",
            "description": "send the audit",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    assert reducer.run_once()["dispositions"] == {"created": 1}
    concern = store.active()[0]
    salience = concern.salience

    restarted = EventConcernReducer(
        store,
        replay_fn=journal.replay,
        current_sequence_fn=journal.current,
    )
    assert restarted.run_once()["processed"] == 0
    assert store.active()[0].concern_id == concern.concern_id
    assert store.active()[0].salience == salience


def test_same_material_state_does_not_inflate_salience(tmp_path):
    journal = FakeJournal([
        event(1, "commitment.overdue", {
            "commitment_id": "cm-1", "person_id": "person-owner",
            "description": "send the audit",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    reducer.run_once()
    first = store.active()[0]

    journal.events.append(event(2, "commitment.overdue", {
        "commitment_id": "cm-1", "person_id": "person-owner",
        "description": "send the audit",
    }))
    result = reducer.run_once()
    second = store.active()[0]
    assert result["dispositions"] == {"duplicate_material": 1}
    assert second.concern_id == first.concern_id
    assert second.salience == first.salience
    assert second.last_material_event_seq == 1


def test_material_change_updates_existing_concern(tmp_path):
    journal = FakeJournal([
        event(1, "service.degraded", {"service_id": "gateway", "reason": "slow"}),
    ])
    store, reducer = make(tmp_path, journal)
    reducer.run_once()
    before = store.active()[0]
    journal.events.append(event(2, "service.degraded", {
        "service_id": "gateway", "reason": "unreachable",
    }))
    result = reducer.run_once()
    after = store.active()[0]
    assert result["dispositions"] == {"updated": 1}
    assert after.concern_id == before.concern_id
    assert after.summary == "unreachable"
    assert after.salience > before.salience
    assert after.last_material_event_seq == 2


def test_terminal_event_resolves_same_concern(tmp_path):
    journal = FakeJournal([
        event(1, "commitment.overdue", {
            "commitment_id": "cm-1", "person_id": "person-owner",
            "description": "send the audit",
        }),
        event(2, "commitment.fulfilled", {
            "commitment_id": "cm-1", "person_id": "person-owner",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    result = reducer.run_once()
    assert result["dispositions"] == {"created": 1, "resolved": 1}
    assert store.active() == []


def test_one_work_order_cannot_resolve_another_steps_concern(tmp_path):
    journal = FakeJournal([
        event(1, "work_order.result", {
            "project_id": "project-1",
            "step_id": "step-1",
            "work_order_id": "work-order-1",
            "status": "failed",
            "summary": "first step failed",
        }),
        event(2, "work_order.result", {
            "project_id": "project-1",
            "step_id": "step-2",
            "work_order_id": "work-order-2",
            "status": "cancelled",
            "summary": "second step cancelled",
        }),
    ])
    store, reducer = make(tmp_path, journal)

    result = reducer.run_once()

    assert result["dispositions"] == {
        "created": 1,
        "resolve_noop": 1,
    }
    active = store.active()
    assert len(active) == 1
    assert active[0].dedup_key.endswith(":work-order-1")
    assert active[0].summary == "first step failed"


def test_two_contacts_with_same_text_remain_separate_and_scoped(tmp_path):
    journal = FakeJournal([
        event(1, "relationship.follow_up_due", {
            "contact_id": "person-a", "description": "follow up",
            "shareability": "subject_private",
        }),
        event(2, "relationship.follow_up_due", {
            "contact_id": "person-b", "description": "follow up",
            "shareability": "subject_private",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    reducer.run_once()
    assert len(store.active()) == 2
    a = store.active_for_viewer(
        viewer_person_id="person-a", owner_person_id="person-owner", limit=10,
    )
    b = store.active_for_viewer(
        viewer_person_id="person-b", owner_person_id="person-owner", limit=10,
    )
    assert [concern.subject_person_id for concern in a] == ["person-a"]
    assert [concern.subject_person_id for concern in b] == ["person-b"]


def test_owner_private_event_is_invisible_to_guest(tmp_path):
    journal = FakeJournal([
        event(1, "surprise.high", {
            "surprise_id": "sur-1", "observation": "private owner signal",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    reducer.run_once()
    assert len(store.active_for_viewer(
        viewer_person_id="person-owner", owner_person_id="person-owner", limit=10,
    )) == 1
    assert store.active_for_viewer(
        viewer_person_id="person-guest", owner_person_id="person-owner", limit=10,
    ) == []


def test_unattested_public_claim_is_downgraded(tmp_path):
    journal = FakeJournal([
        event(1, "relationship.follow_up_due", {
            "contact_id": "person-a", "description": "follow up",
            "shareability": "public",
        }),
    ])
    store, reducer = make(tmp_path, journal)
    reducer.run_once()
    concern = store.active()[0]
    assert concern.shareability == "owner_private"
    assert not concern.visible_to(
        viewer_person_id="person-a",
        owner_person_id="person-owner",
        audiences=frozenset({"global"}),
    )


def test_unknown_and_malformed_events_are_audited_skips(tmp_path):
    journal = FakeJournal([
        event(1, "conversation.turn", {"content": "never a concern"}),
        event(2, "commitment.overdue", ["malformed"]),
    ])
    store, reducer = make(tmp_path, journal)
    result = reducer.run_once()
    assert result["dispositions"] == {"skipped": 2}
    assert result["cursor"] == 2
    assert store.active() == []
    status = reducer.status()
    assert status["dispositions"]["skipped"]["count"] == 2


def test_retention_gap_stops_until_explicit_acknowledgement(tmp_path, monkeypatch):
    journal = FakeJournal([], first_available=0)
    store, reducer = make(tmp_path, journal)
    store.initialize_event_cursor(reducer.consumer_id, 3, bootstrap_mode="replay")
    journal.first_available = 8
    journal.events = [event(8, "service.degraded", {
        "service_id": "gateway", "reason": "unreachable",
    })]
    stopped = reducer.run_once()
    assert stopped["error"] == "journal_retention_gap:3:8"
    assert store.event_cursor(reducer.consumer_id) == 3
    assert store.active() == []

    monkeypatch.setenv("COLONY_EVENT_CONCERNS_GAP_POLICY", "acknowledge")
    resumed = reducer.run_once()
    assert resumed["dispositions"] == {"created": 1}
    status = reducer.status()
    assert status["gaps"]["count"] == 1
    assert status["cursor"] == 8


def test_corrupt_journal_batch_stops_without_advancing(tmp_path):
    journal = FakeJournal([
        event(1, "service.degraded", {
            "service_id": "gateway", "reason": "unreachable",
        }),
    ])

    def corrupt_replay(**kwargs):
        result = journal.replay(**kwargs)
        result["corruptCount"] = 1
        return result

    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = EventConcernReducer(
        store,
        replay_fn=corrupt_replay,
        current_sequence_fn=journal.current,
    )

    stopped = reducer.run_once()

    assert stopped["error"] == "journal_corruption_detected:1"
    assert store.event_cursor(reducer.consumer_id) == 0
    assert store.active() == []
    assert reducer.status()["healthy"] is False


def test_journal_rewind_stops_without_advancing(tmp_path):
    store = ConcernStore(str(tmp_path / "workspace.db"))

    def replay(*, after_seq, **_kwargs):
        assert after_seq == 5
        return {
            "events": [], "firstAvailableSeq": 1,
            "journalLastSeq": 3, "corruptCount": 0, "hasMore": False,
        }

    reducer = EventConcernReducer(
        store,
        replay_fn=replay,
        current_sequence_fn=lambda: 3,
    )
    store.initialize_event_cursor(
        reducer.consumer_id, 5, bootstrap_mode="replay",
    )

    stopped = reducer.run_once()

    assert stopped["error"] == "event_journal_rewind:5:3"
    assert stopped["processed"] == 0
    assert stopped["cursor"] == 5
    assert store.event_cursor(reducer.consumer_id) == 5
    assert reducer.status()["healthy"] is False


def test_internal_concern_sequence_gap_requires_acknowledgement(
    tmp_path, monkeypatch,
):
    journal = FakeJournal([
        event(1, "service.degraded", {
            "service_id": "gateway", "reason": "unreachable",
        }),
        event(3, "service.recovered", {
            "service_id": "gateway", "reason": "healthy",
        }),
    ])
    store, reducer = make(tmp_path, journal)

    stopped = reducer.run_once()
    assert stopped["error"] == "journal_sequence_gap:1:3"
    assert stopped["processed"] == 1
    assert store.event_cursor(reducer.consumer_id) == 1
    assert len(store.active()) == 1

    monkeypatch.setenv("COLONY_EVENT_CONCERNS_GAP_POLICY", "acknowledge")
    resumed = reducer.run_once()
    assert resumed["dispositions"] == {"resolved": 1}
    assert store.event_cursor(reducer.consumer_id) == 3
    assert store.active() == []
    assert reducer.status()["gaps"]["count"] == 1


def test_conflicting_event_id_is_rejected(tmp_path):
    journal = FakeJournal([
        event(1, "service.degraded", {"service_id": "gateway", "reason": "slow"},
              event_id="same-event"),
    ])
    store, reducer = make(tmp_path, journal)
    reducer.run_once()
    with pytest.raises(ValueError, match="conflicting content"):
        store.apply_event(
            consumer_id=reducer.consumer_id,
            event_seq=2,
            event_id="same-event",
            event_type="service.degraded",
            material_digest="f" * 64,
            projection=None,
            skip_reason="test",
        )


def test_workspace_read_and_mutation_have_separate_scopes():
    assert required_scope("GET", "/v1/host/self/workspace") == "cognition:read"
    assert required_scope(
        "POST", "/v1/host/self/workspace/c-123/resolve"
    ) == "cognition:manage"
