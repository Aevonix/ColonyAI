"""P8 append-only conversational arcs and evidence-bound closure."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from colony_sidecar.tom.arcs import (
    ArcConflictError,
    ArcEventV1,
    ArcReducer,
    ArcStore,
)
from colony_sidecar.tom.visibility import ViewerContextV1


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _viewer(person, *, audiences=("viewer",), conversation_scope="dm:alice"):
    return ViewerContextV1(
        principal_id=f"surface:{person}",
        viewer_person_id=person,
        owner_person_id="owner",
        audiences=audiences,
        conversation_scope=conversation_scope,
        scope_revision="scope-rev-1",
        attested=True,
    )


def _open(
    *,
    arc_id="arc:launch",
    event_id="arc-event:open",
    idempotency_key="turn:1:arc:launch:open",
    arc_type="shared_plan",
    topic="launch plan",
    people=("alice", "owner"),
    viewer_scope="person:alice",
    shareability="shared",
    subject_person_id=None,
):
    return ArcEventV1.open(
        event_id=event_id,
        idempotency_key=idempotency_key,
        arc_id=arc_id,
        arc_type=arc_type,
        topic=topic,
        people=people,
        source_turn_ref="turn:1",
        source_ref="turn:1",
        viewer_scope=viewer_scope,
        shareability=shareability,
        subject_person_id=subject_person_id or people[0],
        evidence_refs=("turn:1",),
        occurred_at=NOW.isoformat(),
        commitment_refs=("commitment:launch",),
        expectation_refs=("expectation:reply",),
        project_refs=("project:launch",),
        due_at=(NOW + timedelta(days=1)).isoformat(),
        next_check_at=(NOW + timedelta(hours=4)).isoformat(),
    )


def test_event_and_reduced_arc_are_immutable_and_linked():
    opened = _open()
    linked = ArcEventV1.link(
        event_id="arc-event:link",
        idempotency_key="turn:2:arc:launch:link",
        arc_id=opened.arc_id,
        source_ref="turn:2",
        subject_person_id="bob",
        viewer_scope="person:alice",
        shareability="shared",
        turn_refs=("turn:2",),
        people=("bob",),
        commitment_refs=("commitment:second",),
        evidence_refs=("turn:2",),
        occurred_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    arc = ArcReducer.reduce((opened, linked))
    assert arc.arc_type == "shared_plan"
    assert arc.state == "open"
    assert arc.people == ("alice", "bob", "owner")
    assert arc.turn_refs == ("turn:1", "turn:2")
    assert arc.commitment_refs == (
        "commitment:launch", "commitment:second")
    assert arc.expectation_refs == ("expectation:reply",)
    assert arc.project_refs == ("project:launch",)
    assert arc.event_count == 2
    assert len(arc.history_digest) == 64
    with pytest.raises(FrozenInstanceError):
        arc.state = "closed"  # type: ignore[misc]


def test_close_requires_evidence_and_time_alone_never_closes():
    opened = _open()
    with pytest.raises(ValueError, match="closure evidence"):
        ArcEventV1.close(
            event_id="arc-event:close-bad",
            idempotency_key="arc:launch:close-bad",
            arc_id=opened.arc_id,
            source_ref="clock:deadline",
            subject_person_id="alice",
            viewer_scope="person:alice",
            shareability="shared",
            closure_reason="deadline passed",
            evidence_refs=(),
            occurred_at=(NOW + timedelta(days=2)).isoformat(),
        )

    overdue = ArcReducer.reduce((opened,), now=NOW + timedelta(days=2))
    assert overdue.state == "open"
    assert overdue.overdue is True
    assert overdue.closure_evidence_refs == ()

    closed = ArcReducer.reduce((
        opened,
        ArcEventV1.close(
            event_id="arc-event:close",
            idempotency_key="turn:3:arc:launch:close",
            arc_id=opened.arc_id,
            source_ref="receipt:turn-3",
            subject_person_id="alice",
            viewer_scope="person:alice",
            shareability="shared",
            closure_reason="Alice confirmed the plan is complete",
            evidence_refs=("receipt:turn-3", "turn:3"),
            occurred_at=(NOW + timedelta(hours=1)).isoformat(),
        ),
    ))
    assert closed.state == "closed"
    assert closed.closure_evidence_refs == ("receipt:turn-3", "turn:3")
    assert closed.closed_at == (NOW + timedelta(hours=1)).isoformat()


def test_reducer_rejects_missing_open_scope_mutation_and_post_close_link():
    with pytest.raises(ValueError, match="open event"):
        ArcReducer.reduce((ArcEventV1.link(
            event_id="arc-event:link-only",
            idempotency_key="arc:link-only",
            arc_id="arc:missing",
            source_ref="turn:1",
            subject_person_id="alice",
            viewer_scope="person:alice",
            shareability="shared",
            turn_refs=("turn:1",),
            evidence_refs=("turn:1",),
            occurred_at=NOW.isoformat(),
        ),))

    opened = _open()
    widened = ArcEventV1.link(
        event_id="arc-event:widen",
        idempotency_key="arc:widen",
        arc_id=opened.arc_id,
        source_ref="turn:2",
        subject_person_id="alice",
        viewer_scope="public",
        shareability="public",
        turn_refs=("turn:2",),
        evidence_refs=("turn:2",),
        occurred_at=NOW.isoformat(),
    )
    with pytest.raises(ValueError, match="visibility must exactly match"):
        ArcReducer.reduce((opened, widened))

    bob_private = ArcEventV1.link(
        event_id="arc-event:bob-private",
        idempotency_key="arc:bob-private",
        arc_id=opened.arc_id,
        source_ref="turn:bob-private",
        subject_person_id="bob",
        viewer_scope="person:bob",
        shareability="subject_private",
        people=("bob",),
        turn_refs=("turn:bob-private",),
        evidence_refs=("turn:bob-private",),
        occurred_at=NOW.isoformat(),
    )
    with pytest.raises(ValueError, match="visibility must exactly match"):
        ArcReducer.reduce((opened, bob_private))

    closed = ArcEventV1.close(
        event_id="arc-event:close",
        idempotency_key="arc:close",
        arc_id=opened.arc_id,
        source_ref="receipt:close",
        subject_person_id="alice",
        viewer_scope="person:alice",
        shareability="shared",
        closure_reason="confirmed",
        evidence_refs=("receipt:close",),
        occurred_at=(NOW + timedelta(minutes=1)).isoformat(),
    )
    after = ArcEventV1.link(
        event_id="arc-event:after",
        idempotency_key="arc:after",
        arc_id=opened.arc_id,
        source_ref="turn:after",
        subject_person_id="alice",
        viewer_scope="person:alice",
        shareability="shared",
        turn_refs=("turn:after",),
        evidence_refs=("turn:after",),
        occurred_at=(NOW + timedelta(minutes=2)).isoformat(),
    )
    with pytest.raises(ValueError, match="terminal arc"):
        ArcReducer.reduce((opened, closed, after))


def test_reducer_rejects_unbounded_accumulated_arc_links():
    events = [_open()]
    for index in range(64):
        events.append(ArcEventV1.link(
            event_id=f"event:link:{index}",
            idempotency_key=f"arc:launch:link:{index}",
            arc_id="arc:launch",
            source_ref=f"turn:link:{index}",
            subject_person_id="alice",
            viewer_scope="person:alice",
            shareability="shared",
            turn_refs=(f"turn:link:{index}",),
            evidence_refs=(f"turn:link:{index}",),
            occurred_at=(NOW + timedelta(seconds=index + 1)).isoformat(),
        ))
    with pytest.raises(ValueError, match="bounded limit"):
        ArcReducer.reduce(tuple(events))


def test_store_is_append_only_and_idempotent_with_conflict_detection(tmp_path):
    path = tmp_path / "arcs.db"
    store = ArcStore(str(path))
    event = _open()
    first = store.append(event)
    replay = store.append(event)
    assert first.appended is True and first.replayed is False
    assert replay.appended is False and replay.replayed is True
    assert first.event.sequence == replay.event.sequence == 1
    assert store.event_count() == 1

    changed = _open(topic="changed topic")
    with pytest.raises(ArcConflictError, match="idempotency"):
        store.append(changed)

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE arc_events SET source_ref='changed' WHERE seq=1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM arc_events WHERE seq=1")


def test_store_concurrent_replay_appends_exactly_once(tmp_path):
    store = ArcStore(str(tmp_path / "arcs.db"))
    event = _open()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: store.append(event), range(40)))

    assert sum(1 for result in results if result.appended) == 1
    assert sum(1 for result in results if result.replayed) == 39
    assert store.event_count() == 1
    assert {result.event.sequence for result in results} == {1}


def test_store_concurrent_replay_across_connections_appends_once(tmp_path):
    path = tmp_path / "arcs.db"
    stores = tuple(ArcStore(str(path)) for _ in range(4))
    event = _open()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(
            lambda index: stores[index % len(stores)].append(event),
            range(40),
        ))

    assert sum(1 for result in results if result.appended) == 1
    assert sum(1 for result in results if result.replayed) == 39
    assert {store.event_count() for store in stores} == {1}
    assert {result.event.sequence for result in results} == {1}


def test_store_restart_replay_and_projection_are_stable(tmp_path):
    path = tmp_path / "arcs.db"
    first = ArcStore(str(path))
    first.append(_open())
    first.append(ArcEventV1.link(
        event_id="arc-event:link",
        idempotency_key="arc:link",
        arc_id="arc:launch",
        source_ref="turn:2",
        subject_person_id="alice",
        viewer_scope="person:alice",
        shareability="shared",
        turn_refs=("turn:2",),
        evidence_refs=("turn:2",),
        occurred_at=(NOW + timedelta(minutes=1)).isoformat(),
    ))
    before = first.get_arc("arc:launch", now=NOW)
    first.close()

    restarted = ArcStore(str(path))
    replay = restarted.append(_open())
    after = restarted.get_arc("arc:launch", now=NOW)
    assert replay.replayed is True
    assert before == after


def test_arc_projection_is_scoped_active_and_bounded(tmp_path):
    store = ArcStore(str(tmp_path / "arcs.db"))
    store.append(_open())
    store.append(_open(
        arc_id="arc:bob", event_id="arc-event:bob",
        idempotency_key="arc:bob:open", topic="Bob private topic",
        people=("bob",), viewer_scope="person:bob",
        shareability="subject_private", arc_type="stress_topic"))
    store.append(_open(
        arc_id="arc:alice-2", event_id="arc-event:alice-2",
        idempotency_key="arc:alice-2:open", topic="follow up two",
        people=("alice",), arc_type="follow_up"))
    store.append(ArcEventV1.close(
        event_id="arc-event:alice-2-close",
        idempotency_key="arc:alice-2:close",
        arc_id="arc:alice-2",
        source_ref="receipt:done",
        subject_person_id="alice",
        viewer_scope="person:alice",
        shareability="shared",
        closure_reason="done",
        evidence_refs=("receipt:done",),
        occurred_at=(NOW + timedelta(minutes=1)).isoformat(),
    ))

    alice = store.project_active(_viewer("alice"), now=NOW, max_arcs=5)
    assert [arc.arc_id for arc in alice.arcs] == ["arc:launch"]
    assert "Bob private topic" not in repr(alice.public())
    assert alice.denied_count >= 1
    assert len(alice.audit_digest) == 64

    bounded = store.project_active(_viewer("owner"), now=NOW, max_arcs=1)
    assert len(bounded.arcs) == 1
    assert bounded.truncated is True


@pytest.mark.parametrize(
    "arc_type",
    [
        "promise", "open_question", "stress_topic", "decision",
        "shared_plan", "follow_up", "unresolved_social_moment",
    ],
)
def test_required_arc_types_are_versioned(arc_type):
    arc = ArcReducer.reduce((_open(
        arc_id=f"arc:{arc_type}", event_id=f"event:{arc_type}",
        idempotency_key=f"idempotency:{arc_type}", arc_type=arc_type),))
    assert arc.schema_version == 1
    assert arc.arc_type == arc_type
