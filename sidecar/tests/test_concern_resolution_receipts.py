"""Immutable owner concern-resolution receipts and Operator Deck semantics."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import json
import sqlite3

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.api.authority import RequestAuthority, required_scope
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.self_model import settlement
from colony_sidecar.self_model.workspace import (
    RECENT_RESOLUTIONS_LIMIT,
    ConcernResolutionConflict,
    ConcernStore,
    WorkspaceEngine,
)


@pytest.fixture(autouse=True)
def _isolate_settlers():
    saved = dict(settlement._SETTLERS)
    saved_retry_safe = set(settlement._RETRY_SAFE)
    settlement._SETTLERS.clear()
    settlement._RETRY_SAFE.clear()
    yield
    settlement._SETTLERS.clear()
    settlement._SETTLERS.update(saved)
    settlement._RETRY_SAFE.clear()
    settlement._RETRY_SAFE.update(saved_retry_safe)


def _wire_retry_safe_commitments(store, calls=None):
    def _settle(
        source_id, *, outcome="done", note="", resolved_by="owner",
        operation_id=None,
    ):
        if calls is not None:
            calls.append((source_id, operation_id))
        row = store.resolve(
            source_id,
            outcome=outcome,
            note=note,
            resolved_by=resolved_by,
            operation_id=operation_id,
        )
        if not row:
            return None
        operation = (
            store.get_resolution_operation(source_id)
            if operation_id is not None else None
        )
        resolution = operation or (
            (row.get("metadata") or {}).get("resolution") or {}
        )
        return {
            "kind": "commitment",
            "status": row["status"],
            "operation_id": resolution.get("operation_id"),
            "outcome": resolution.get("outcome"),
            "note_digest": resolution.get("note_digest"),
            "resolved_by": (
                resolution.get("resolved_by")
                if operation is not None else resolution.get("by")
            ),
        }

    settlement.register_settler("commitment", _settle, retry_safe=True)


def _authority(viewer: str, *, audiences=("viewer",)) -> RequestAuthority:
    return RequestAuthority(
        principal_id=f"principal-{viewer}",
        credential_id=f"credential-{viewer}",
        scopes=frozenset({"cognition:read", "cognition:manage"}),
        viewer_person_id=viewer,
        person_ids=frozenset({viewer}),
        audiences=frozenset(audiences),
        authenticated=True,
        allow_unscoped_api=False,
    )


@asynccontextmanager
async def _client(workspace: WorkspaceEngine, authority: RequestAuthority):
    original = host_mod._workspace
    host_mod._workspace = workspace
    app = FastAPI()

    @app.middleware("http")
    async def _scoped_authority(request, call_next):
        request.state.colony_authority = authority
        return await call_next(request)

    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            yield client
    finally:
        host_mod._workspace = original


def _old_workspace(path, *, status="resolved", note="handled before upgrade"):
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE concerns (
            concern_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            salience REAL NOT NULL,
            sources TEXT,
            dedup_key TEXT,
            thoughts_spent INTEGER DEFAULT 0,
            max_thoughts INTEGER DEFAULT 8,
            status TEXT DEFAULT 'active',
            last_note TEXT,
            created_at REAL NOT NULL,
            last_touched REAL NOT NULL,
            last_thought_at REAL
        )"""
    )
    connection.execute(
        """INSERT INTO concerns
           (concern_id,kind,summary,salience,sources,dedup_key,thoughts_spent,
            max_thoughts,status,last_note,created_at,last_touched,last_thought_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "concern-legacy", "thread", "legacy concern", 0.0, "[]",
            "legacy-key", 1, 8, status, note, 1000.0, 1234.5, 1234.5,
        ),
    )
    connection.commit()
    connection.close()


def test_additive_migration_freezes_truthful_legacy_receipt(tmp_path):
    path = tmp_path / "legacy-workspace.db"
    _old_workspace(path)

    store = ConcernStore(str(path))
    receipt = store.get_resolution("concern-legacy")
    assert receipt is not None
    assert receipt["schema"] == "ColonyConcernResolutionReceiptV1"
    assert receipt["provenance"] == "legacy_unrecorded"
    assert receipt["outcome"] is None
    assert receipt["cascade"] is None
    assert receipt["resolved_by"] is None
    assert receipt["note"] == "handled before upgrade"
    assert receipt["note_digest"] == hashlib.sha256(
        b"handled before upgrade"
    ).hexdigest()
    assert receipt["cascade_evidence"]["status"] == "legacy_unknown"
    assert receipt["cascade_evidence"]["source_capture"] == "migration_snapshot"
    columns = {
        row[1]
        for row in store._conn.execute(
            "PRAGMA table_info(concern_resolutions)"
        ).fetchall()
    }
    assert columns == {
        "resolution_id", "concern_id", "outcome", "note", "note_digest",
        "cascade", "resolved_by", "resolved_at", "provenance",
        "record_digest",
    }
    first_identity = receipt["resolution_id"]
    store._conn.close()

    reopened = ConcernStore(str(path))
    assert reopened.get_resolution("concern-legacy")["resolution_id"] == first_identity
    assert reopened.get("concern-legacy").status == "resolved"


async def test_owner_scoped_exact_get_and_scope_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="question", summary="owner concern", dedup_key="owner-concern",
    )
    payload = {
        "note": "owner chose the exact outcome",
        "outcome": "done",
        "cascade": True,
        "resolved_by": "owner-deck",
    }

    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        resolved = await client.post(
            f"/v1/host/self/workspace/{concern.concern_id}/resolve",
            json=payload,
        )
        assert resolved.status_code == 200
        fetched = await client.get(
            f"/v1/host/self/workspace/{concern.concern_id}/resolution"
        )
        assert fetched.status_code == 200
        assert fetched.json()["resolution"] == resolved.json()["resolution"]

    async with _client(workspace, _authority("person-other")) as client:
        hidden = await client.get(
            f"/v1/host/self/workspace/{concern.concern_id}/resolution"
        )
        assert hidden.status_code == 404
        assert hidden.json()["detail"] == "no concern with that id"

    path = f"/v1/host/self/workspace/{concern.concern_id}/resolution"
    assert required_scope("GET", path) == "cognition:read"
    assert required_scope(
        "POST", f"/v1/host/self/workspace/{concern.concern_id}/resolve",
    ) == "cognition:manage"


async def test_resolution_receipt_is_exact_and_replay_is_idempotent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal", summary="finish exact work", dedup_key="exact-work",
    )
    payload = {
        "note": "Finished with receipt-bound evidence.",
        "outcome": "done",
        "cascade": True,
        "resolved_by": "operator-v3",
    }

    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        first = await client.post(
            f"/v1/host/self/workspace/{concern.concern_id}/resolve",
            json=payload,
        )
        replay = await client.post(
            f"/v1/host/self/workspace/{concern.concern_id}/resolve",
            json=payload,
        )

    assert first.status_code == replay.status_code == 200
    assert first.json()["already_resolved"] is False
    assert replay.json()["already_resolved"] is True
    receipt = first.json()["resolution"]
    assert replay.json()["resolution"] == receipt
    assert receipt["concern_id"] == concern.concern_id
    assert receipt["outcome"] == payload["outcome"]
    assert receipt["note"] == payload["note"]
    assert receipt["cascade"] is True
    assert receipt["resolved_by"] == payload["resolved_by"]
    assert receipt["provenance"] == "owner_api"
    assert receipt["cascade_evidence"]["status"] == "not_applicable"
    assert receipt["note_digest"] == hashlib.sha256(
        payload["note"].encode("utf-8")
    ).hexdigest()
    digest_payload = {
        name: receipt[name]
        for name in (
            "schema", "version", "concern_id", "outcome", "note",
            "note_digest", "cascade", "resolved_by", "resolved_at",
            "provenance",
        )
    }
    expected_digest = hashlib.sha256(json.dumps(
        digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    assert receipt["record_digest"] == expected_digest
    assert receipt["resolution_id"] == f"concern-resolution:{expected_digest}"
    assert store.get(concern.concern_id).thoughts_spent == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolutions WHERE concern_id=?",
        (concern.concern_id,),
    ).fetchone()[0] == 1

    with store._lock:
        store._conn.execute(
            "UPDATE concern_resolutions SET note=? WHERE concern_id=?",
            ("tampered note", concern.concern_id),
        )
        store._conn.commit()
    with pytest.raises(ValueError, match="receipt integrity check failed"):
        store.get_resolution(concern.concern_id)


async def test_conflicting_replay_returns_first_receipt_not_second_claim(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="thread", summary="one terminal meaning", dedup_key="one-meaning",
    )
    first_payload = {
        "note": "The request was completed.",
        "outcome": "done",
        "cascade": True,
        "resolved_by": "operator-v3",
    }
    conflicting_payloads = (
        {**first_payload, "outcome": "invalid"},
        {**first_payload, "note": "This was never a valid request."},
        {**first_payload, "cascade": False},
        {**first_payload, "resolved_by": "second-operator"},
    )

    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        first = await client.post(
            f"/v1/host/self/workspace/{concern.concern_id}/resolve",
            json=first_payload,
        )
        conflicts = [
            await client.post(
                f"/v1/host/self/workspace/{concern.concern_id}/resolve",
                json=payload,
            )
            for payload in conflicting_payloads
        ]

    assert first.status_code == 200
    for conflict in conflicts:
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert detail["code"] == "concern_resolution_replay_conflict"
        assert detail["resolution"] == first.json()["resolution"]
        assert detail["resolution"]["outcome"] == "done"
        assert detail["resolution"]["note"] == "The request was completed."
        assert detail["resolution"]["cascade"] is True
        assert detail["resolution"]["resolved_by"] == "operator-v3"
        assert "invalid" not in json.dumps(detail)
        assert "second-operator" not in json.dumps(detail)


async def test_concurrent_exact_http_replay_runs_source_settler_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal",
        summary="settle one source once",
        dedup_key="settle-source-once",
        sources=["test-source:item-1"],
    )
    calls = []

    def _settle_once(sources, **decision):
        calls.append((tuple(sources), dict(decision)))
        return [{"source": sources[0], "settled": True, "kind": "test-source"}]

    monkeypatch.setattr(settlement, "settle_sources", _settle_once)
    payload = {
        "note": "one exact owner decision",
        "outcome": "done",
        "cascade": True,
        "resolved_by": "operator-v3",
    }
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        responses = await asyncio.gather(
            client.post(path, json=payload),
            client.post(path, json=payload),
        )

    assert [response.status_code for response in responses] == [200, 200]
    documents = [response.json() for response in responses]
    assert sorted(document["already_resolved"] for document in documents) == [
        False, True,
    ]
    assert len(calls) == 1
    assert {document["cascade_status"] for document in documents} == {"succeeded"}
    assert {
        document["resolution"]["cascade_evidence"]["status"]
        for document in documents
    } == {"succeeded"}
    assert calls[0][0] == ("test-source:item-1",)
    assert calls[0][1]["operation_root"] == documents[0]["resolution"][
        "cascade_evidence"
    ]["intent_id"]
    assert {
        key: value for key, value in calls[0][1].items()
        if key != "operation_root"
    } == {
        "outcome": "done",
        "note": "one exact owner decision",
        "resolved_by": "operator-v3",
    }
    assert sorted(len(document["settled_sources"]) for document in documents) == [
        0, 1,
    ]
    assert store.get(concern.concern_id).thoughts_spent == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolutions WHERE concern_id=?",
        (concern.concern_id,),
    ).fetchone()[0] == 1


async def test_concurrent_conflicting_http_loser_has_no_settler_or_write(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="thread",
        summary="one winning terminal claim",
        dedup_key="one-winning-terminal-claim",
        sources=["test-source:item-2"],
    )
    calls = []

    def _settle_winner(sources, **decision):
        calls.append((tuple(sources), dict(decision)))
        return [{"source": sources[0], "settled": True, "kind": "test-source"}]

    monkeypatch.setattr(settlement, "settle_sources", _settle_winner)
    base = {
        "note": "concurrent owner decision",
        "cascade": True,
        "resolved_by": "operator-v3",
    }
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        responses = await asyncio.gather(
            client.post(path, json={**base, "outcome": "done"}),
            client.post(path, json={**base, "outcome": "invalid"}),
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(
        response.json() for response in responses if response.status_code == 200
    )
    loser = next(
        response.json() for response in responses if response.status_code == 409
    )
    assert len(calls) == 1
    assert calls[0][1]["outcome"] == winner["resolution"]["outcome"]
    assert loser["detail"]["resolution"] == winner["resolution"]
    assert store.get(concern.concern_id).thoughts_spent == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolutions WHERE concern_id=?",
        (concern.concern_id,),
    ).fetchone()[0] == 1


async def test_legacy_terminal_row_never_adopts_later_callers_outcome(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    path = tmp_path / "legacy-workspace.db"
    _old_workspace(path)
    store = ConcernStore(str(path))
    workspace = WorkspaceEngine(store)

    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        fetched = await client.get(
            "/v1/host/self/workspace/concern-legacy/resolution"
        )
        claimed = await client.post(
            "/v1/host/self/workspace/concern-legacy/resolve",
            json={
                "note": "later caller's story",
                "outcome": "invalid",
                "resolved_by": "later-caller",
            },
        )

    assert fetched.status_code == 200
    legacy = fetched.json()["resolution"]
    assert legacy["provenance"] == "legacy_unrecorded"
    assert legacy["outcome"] is None
    assert legacy["cascade"] is None
    assert legacy["resolved_by"] is None
    assert claimed.status_code == 409
    detail = claimed.json()["detail"]
    assert detail["code"] == "legacy_concern_resolution_conflict"
    assert detail["resolution"] == legacy
    assert "later caller" not in json.dumps(detail)
    assert store.get_resolution("concern-legacy") == legacy

    unrestricted = workspace.snapshot(unrestricted=True)
    assert unrestricted["recent_resolutions"] == [legacy]


def test_recent_resolutions_are_bounded_validated_and_newest_first(tmp_path):
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    created = []
    for index in range(RECENT_RESOLUTIONS_LIMIT + 5):
        concern = workspace.bump(
            kind="thread",
            summary=f"resolved concern {index}",
            dedup_key=f"resolved-concern-{index}",
        )
        receipt, first = store.resolve_with_owner_record(
            concern.concern_id,
            outcome="done",
            note=f"resolved note {index}",
            cascade=bool(index % 2),
            resolved_by="owner-test",
        )
        assert first is True
        created.append(receipt)

    snapshot = workspace.snapshot(unrestricted=True)
    assert {"mode", "capacity", "sleeping", "concerns"}.issubset(snapshot)
    recent = snapshot["recent_resolutions"]
    assert len(recent) == RECENT_RESOLUTIONS_LIMIT
    assert [
        (receipt["resolved_at"], receipt["resolution_id"])
        for receipt in recent
    ] == sorted(
        (
            (receipt["resolved_at"], receipt["resolution_id"])
            for receipt in created
        ),
        reverse=True,
    )[:RECENT_RESOLUTIONS_LIMIT]
    assert store.recent_resolutions(10_000) == recent

    with store._lock:
        store._conn.execute(
            "UPDATE concern_resolutions SET cascade=7 WHERE concern_id=?",
            (recent[0]["concern_id"],),
        )
        store._conn.commit()
    with pytest.raises(ValueError, match="receipt cascade is invalid"):
        workspace.snapshot(unrestricted=True)


async def test_recent_resolutions_only_appear_for_legacy_or_exact_owner(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    owner_concern = workspace.bump(
        kind="goal", summary="owner resolution", dedup_key="owner-resolution",
    )
    subject_concern = workspace.bump(
        kind="thread", summary="subject visible", dedup_key="subject-visible",
    )
    with store._lock:
        store._conn.execute(
            """UPDATE concerns SET subject_person_id=?,shareability=?,viewer_scope=?
               WHERE concern_id=?""",
            (
                "person-subject", "subject_private", "person:person-subject",
                subject_concern.concern_id,
            ),
        )
        store._conn.commit()
    path = f"/v1/host/self/workspace/{owner_concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        owner_resolution = await client.post(path, json={
            "note": "owner-only receipt",
            "outcome": "done",
            "cascade": False,
            "resolved_by": "operator-v3",
        })
        assert owner_resolution.status_code == 200
        owner_snapshot = (await client.get("/v1/host/self/workspace")).json()
    assert len(owner_snapshot["recent_resolutions"]) == 1
    assert owner_resolution.json()["cascade_status"] == "not_requested"

    async with _client(workspace, _authority("person-subject")) as client:
        subject_snapshot = (await client.get("/v1/host/self/workspace")).json()
    assert [row["concern_id"] for row in subject_snapshot["concerns"]] == [
        subject_concern.concern_id,
    ]
    assert "recent_resolutions" not in subject_snapshot

    async with _client(workspace, _authority("person-other")) as client:
        cross_owner = (await client.get("/v1/host/self/workspace")).json()
    assert "recent_resolutions" not in cross_owner

    async with _client(workspace, _authority("person-owner")) as client:
        owner_without_audience = (
            await client.get("/v1/host/self/workspace")
        ).json()
    assert "recent_resolutions" not in owner_without_audience

    legacy_projection = workspace.snapshot(unrestricted=True)
    assert legacy_projection["recent_resolutions"] == owner_snapshot[
        "recent_resolutions"
    ]


async def test_workspace_and_legacy_deck_response_remain_compatible(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="maintenance", summary="deck concern", dedup_key="deck-concern",
    )

    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        snapshot = await client.get("/v1/host/self/workspace")
        resolved = await client.post(
            f"/v1/host/self/workspace/{concern.concern_id}/resolve"
        )
        after = await client.get("/v1/host/self/workspace")

    assert snapshot.status_code == 200
    assert snapshot.json()["concerns"][0]["concern_id"] == concern.concern_id
    assert resolved.status_code == 200
    assert {
        "available", "resolved", "outcome", "already_resolved",
        "settled_sources",
    }.issubset(resolved.json())
    assert resolved.json()["outcome"] == "done"
    assert resolved.json()["cascade_status"] == "not_applicable"
    assert after.status_code == 200
    assert after.json()["concerns"] == []


def test_transactional_pending_intent_survives_restart_with_exact_sources(tmp_path):
    path = tmp_path / "workspace.db"
    store = ConcernStore(str(path))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal",
        summary="pending source work",
        dedup_key="pending-source-work",
        sources=["task:item-1", "goal:item-2"],
    )
    pending, created = store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="owner decision was durable before callback",
        cascade=True,
        resolved_by="owner-test",
    )
    assert created is True
    base_identity = (pending["resolution_id"], pending["record_digest"])
    evidence = pending["cascade_evidence"]
    assert evidence["status"] == "pending"
    assert evidence["source_refs"] == ["task:item-1", "goal:item-2"]
    assert evidence["source_capture"] == "resolution_transaction"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_intents"
    ).fetchone()[0] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 0
    store._conn.close()

    reopened = ConcernStore(str(path))
    after = reopened.get_resolution(concern.concern_id)
    assert (after["resolution_id"], after["record_digest"]) == base_identity
    assert after["cascade_evidence"] == evidence
    assert reopened.recent_resolutions()[0]["cascade_evidence"] == evidence
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 0


def test_pre_feature_base_receipt_migrates_once_to_stable_unknown(tmp_path):
    path = tmp_path / "workspace.db"
    store = ConcernStore(str(path))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="thread",
        summary="old cascade decision",
        dedup_key="old-cascade-decision",
        sources=["commitment:old-1"],
    )
    original, _ = store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="recorded before cascade evidence shipped",
        cascade=True,
        resolved_by="owner-test",
    )
    with store._lock:
        store._conn.execute("DELETE FROM concern_resolution_cascade_receipts")
        store._conn.execute("DELETE FROM concern_resolution_cascade_intents")
        store._conn.commit()
    store._conn.close()

    migrated = ConcernStore(str(path))
    receipt = migrated.get_resolution(concern.concern_id)
    assert receipt["resolution_id"] == original["resolution_id"]
    assert receipt["record_digest"] == original["record_digest"]
    assert receipt["cascade_evidence"]["status"] == "legacy_unknown"
    assert receipt["cascade_evidence"]["source_capture"] == "migration_snapshot"
    first_evidence = receipt["cascade_evidence"]
    migrated._conn.close()

    reopened = ConcernStore(str(path))
    assert reopened.get_resolution(concern.concern_id)[
        "cascade_evidence"
    ] == first_evidence


async def test_failed_settler_is_durable_actionable_and_redacted(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal",
        summary="source callback fails",
        dedup_key="source-callback-fails",
        sources=["failure-test:item-1"],
    )
    calls = []

    def _fail(source_id, **decision):
        calls.append((source_id, decision))
        raise RuntimeError("private failure material must not escape")

    monkeypatch.setitem(settlement._SETTLERS, "failure-test", _fail)
    payload = {
        "note": "resolve with a source failure",
        "outcome": "done",
        "cascade": True,
        "resolved_by": "operator-v3",
    }
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        first = await client.post(path, json=payload)
        fetched = await client.get(
            f"/v1/host/self/workspace/{concern.concern_id}/resolution"
        )
        snapshot = await client.get("/v1/host/self/workspace")
        replay = await client.post(path, json=payload)

    assert first.status_code == fetched.status_code == replay.status_code == 200
    assert first.json()["cascade_status"] == "failed"
    evidence = first.json()["resolution"]["cascade_evidence"]
    assert evidence["status"] == "failed"
    assert evidence["failed_sources"] == ["failure-test:item-1"]
    assert evidence["settled_sources"] == []
    assert evidence["failure_codes"] == ["settler_error"]
    assert len(evidence["failure_digest"]) == 64
    assert fetched.json()["resolution"]["cascade_evidence"] == evidence
    assert snapshot.json()["recent_resolutions"][0]["cascade_evidence"] == evidence
    assert replay.json()["resolution"]["cascade_evidence"] == evidence
    assert replay.json()["already_resolved"] is True
    assert len(calls) == 1
    assert "private failure material" not in json.dumps({
        "first": first.json(),
        "fetched": fetched.json(),
        "snapshot": snapshot.json(),
        "replay": replay.json(),
    })


async def test_outer_settlement_failure_records_failed_evidence_without_raw_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal",
        summary="outer callback failure",
        dedup_key="outer-callback-failure",
        sources=["outer-test:item-1"],
    )
    calls = 0

    def _outer_failure(sources, **decision):
        nonlocal calls
        calls += 1
        raise RuntimeError("outer private material")

    monkeypatch.setattr(settlement, "settle_sources", _outer_failure)
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        first = await client.post(path, json={"cascade": True})
        replay = await client.post(path, json={"cascade": True})

    assert first.status_code == replay.status_code == 200
    evidence = first.json()["resolution"]["cascade_evidence"]
    assert evidence["status"] == "failed"
    assert evidence["failed_sources"] == ["outer-test:item-1"]
    assert evidence["failure_codes"] == ["execution_error", "missing_result"]
    assert replay.json()["resolution"]["cascade_evidence"] == evidence
    assert calls == 1
    assert "outer private material" not in json.dumps(first.json())


def test_malformed_duplicate_and_unknown_results_fail_closed(tmp_path):
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal",
        summary="strict result validation",
        dedup_key="strict-result-validation",
        sources=["task:item-1", "task:item-2"],
    )
    pending, _ = store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="strict result matrix",
        cascade=True,
        resolved_by="owner-test",
    )
    base_identity = (pending["resolution_id"], pending["record_digest"])
    terminal = store.finalize_owner_cascade(
        concern.concern_id,
        results=[
            {"source": "task:item-1", "settled": True},
            {"source": "task:item-1", "settled": True},
            {"source": "other:item", "settled": True},
            "not-a-result",
        ],
    )
    evidence = terminal["cascade_evidence"]
    assert (terminal["resolution_id"], terminal["record_digest"]) == base_identity
    assert evidence["status"] == "failed"
    assert evidence["settled_sources"] == ["task:item-1"]
    assert evidence["failed_sources"] == ["task:item-2"]
    assert evidence["unexpected_result_count"] == 3
    assert evidence["failure_codes"] == [
        "duplicate_result", "malformed_result", "missing_result", "unknown_result",
    ]
    with pytest.raises(
        ConcernResolutionConflict,
        match="immutable cascade outcome replay mismatch",
    ):
        store.finalize_owner_cascade(
            concern.concern_id,
            results=[
                {"source": "task:item-1", "settled": True},
                {"source": "task:item-2", "settled": True},
            ],
        )


async def test_write_failure_recovers_exact_multi_source_operations_on_same_post(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    commitments = CommitmentStore(tmp_path / "commitments.db")
    first_source = commitments.create(person_id="owner", description="first")
    second_source = commitments.create(person_id="owner", description="second")
    source_refs = [
        f"commitment:{first_source['id']}",
        f"commitment:{second_source['id']}",
    ]
    concern = workspace.bump(
        kind="goal",
        summary="receipt write failure",
        dedup_key="receipt-write-failure",
        sources=source_refs,
    )
    calls = []
    _wire_retry_safe_commitments(commitments, calls)

    def _fail_write(receipt):
        raise sqlite3.OperationalError("simulated receipt write failure")

    original_insert = store._insert_cascade_receipt
    monkeypatch.setattr(store, "_insert_cascade_receipt", _fail_write)
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        first = await client.post(path, json={"cascade": True})
        first_resolution_times = [
            commitments.get(source["id"])["metadata"]["resolution"]["at"]
            for source in (first_source, second_source)
        ]
        monkeypatch.setattr(store, "_insert_cascade_receipt", original_insert)
        recovery = await client.post(path, json={"cascade": True})
        terminal_replay = await client.post(path, json={"cascade": True})

    assert first.status_code == 500
    assert first.json()["detail"] == {
        "code": "concern_cascade_evidence_unavailable",
        "message": "cascade outcome could not be recorded",
    }
    assert recovery.status_code == terminal_replay.status_code == 200
    assert recovery.json()["already_resolved"] is True
    assert recovery.json()["recovery_attempted"] is True
    assert recovery.json()["cascade_status"] == "succeeded"
    assert recovery.json()["resolution"]["cascade_evidence"][
        "settled_sources"
    ] == source_refs
    assert terminal_replay.json()["recovery_attempted"] is False
    assert terminal_replay.json()["cascade_status"] == "succeeded"
    assert len(calls) == 4
    assert [source_id for source_id, _ in calls] == [
        first_source["id"], second_source["id"],
        first_source["id"], second_source["id"],
    ]
    assert calls[0][1] == calls[2][1]
    assert calls[1][1] == calls[3][1]
    assert calls[0][1] != calls[1][1]
    assert [
        commitments.get(source["id"])["metadata"]["resolution"]["at"]
        for source in (first_source, second_source)
    ] == first_resolution_times
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 1


async def test_pending_replay_with_unsafe_settler_is_409_without_callback(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="goal",
        summary="unsafe recovery",
        dedup_key="unsafe-recovery",
        sources=["unsafe:item-1"],
    )
    pending, _ = store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="resolved by owner",
        cascade=True,
        resolved_by="owner",
    )
    calls = 0

    def _unsafe(source_id, **decision):
        nonlocal calls
        calls += 1
        return {"kind": "unsafe"}

    settlement.register_settler("unsafe", _unsafe)
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        response = await client.post(path, json={"cascade": True})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "concern_cascade_reconciliation_required"
    assert detail["unsafe_sources"] == ["unsafe:item-1"]
    assert detail["resolution"] == pending
    assert detail["resolution"]["cascade_evidence"]["status"] == "pending"
    assert calls == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 0


async def test_restart_recovers_partial_exact_commitment_cascade(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    workspace_path = tmp_path / "workspace.db"
    store = ConcernStore(str(workspace_path))
    workspace = WorkspaceEngine(store)
    commitments = CommitmentStore(tmp_path / "commitments.db")
    first_source = commitments.create(person_id="owner", description="first")
    second_source = commitments.create(person_id="owner", description="second")
    refs = [
        f"commitment:{first_source['id']}",
        f"commitment:{second_source['id']}",
    ]
    concern = workspace.bump(
        kind="goal",
        summary="partial cascade before crash",
        dedup_key="partial-cascade-before-crash",
        sources=refs,
    )
    pending, _ = store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="recover exact partial work",
        cascade=True,
        resolved_by="operator-v3",
    )
    intent_id = pending["cascade_evidence"]["intent_id"]
    first_operation = settlement.source_operation_id(intent_id, refs[0])
    first_applied = commitments.resolve(
        first_source["id"],
        outcome="done",
        note="recover exact partial work",
        resolved_by="operator-v3",
        operation_id=first_operation,
    )
    first_applied_at = first_applied["metadata"]["resolution"]["at"]
    store._conn.close()

    reopened = ConcernStore(str(workspace_path))
    reopened_workspace = WorkspaceEngine(reopened)
    calls = []
    _wire_retry_safe_commitments(commitments, calls)
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        reopened_workspace,
        _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        recovered = await client.post(path, json={
            "note": "recover exact partial work",
            "outcome": "done",
            "cascade": True,
            "resolved_by": "operator-v3",
        })

    assert recovered.status_code == 200
    assert recovered.json()["recovery_attempted"] is True
    assert recovered.json()["cascade_status"] == "succeeded"
    assert recovered.json()["resolution"]["cascade_evidence"][
        "settled_sources"
    ] == refs
    assert len(calls) == 2
    assert commitments.get(first_source["id"])["metadata"]["resolution"][
        "at"
    ] == first_applied_at
    assert commitments.get(second_source["id"])["status"] == "fulfilled"


async def test_recovery_never_certifies_a_different_terminal_commitment(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = WorkspaceEngine(store)
    commitments = CommitmentStore(tmp_path / "commitments.db")
    source = commitments.create(person_id="owner", description="conflict")
    ref = f"commitment:{source['id']}"
    concern = workspace.bump(
        kind="goal",
        summary="conflicting source terminal",
        dedup_key="conflicting-source-terminal",
        sources=[ref],
    )
    store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="expected done operation",
        cascade=True,
        resolved_by="operator-v3",
    )
    commitments.resolve(
        source["id"], outcome="invalid", note="different direct decision",
        resolved_by="another-actor",
    )
    _wire_retry_safe_commitments(commitments)
    path = f"/v1/host/self/workspace/{concern.concern_id}/resolve"
    async with _client(
        workspace, _authority("person-owner", audiences=("viewer", "owner")),
    ) as client:
        response = await client.post(path, json={
            "note": "expected done operation",
            "outcome": "done",
            "cascade": True,
            "resolved_by": "operator-v3",
        })

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "concern_cascade_reconciliation_required"
    evidence = detail["resolution"]["cascade_evidence"]
    assert evidence["status"] == "pending"
    assert detail["settled_sources"][0]["source"] == ref
    assert detail["settled_sources"][0]["settled"] is False
    assert detail["settled_sources"][0]["error"] == "operation_conflict"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 0
    assert commitments.get(source["id"])["status"] == "cancelled"


def test_concurrent_recoverers_share_exact_source_operation_and_one_receipt(tmp_path):
    workspace_path = tmp_path / "workspace.db"
    first_store = ConcernStore(str(workspace_path))
    second_store = ConcernStore(str(workspace_path))
    workspace = WorkspaceEngine(first_store)
    commitments = CommitmentStore(tmp_path / "commitments.db")
    source = commitments.create(person_id="owner", description="race")
    ref = f"commitment:{source['id']}"
    concern = workspace.bump(
        kind="goal",
        summary="concurrent cascade recovery",
        dedup_key="concurrent-cascade-recovery",
        sources=[ref],
    )
    pending, _ = first_store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="one recovery operation",
        cascade=True,
        resolved_by="operator-v3",
    )
    calls = []
    _wire_retry_safe_commitments(commitments, calls)

    def _recover(store):
        receipt, created = store.resolve_with_owner_record(
            concern.concern_id,
            outcome="done",
            note="one recovery operation",
            cascade=True,
            resolved_by="operator-v3",
        )
        assert created is False
        state = receipt["cascade_evidence"]
        results = settlement.retry_safe_settle_sources(
            state["source_refs"],
            operation_root=state["intent_id"],
            outcome=receipt["outcome"],
            note=receipt["note"],
            resolved_by=receipt["resolved_by"],
        )
        return store.finalize_owner_cascade(
            concern.concern_id, results=results,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovered = list(pool.map(_recover, (first_store, second_store)))
    assert recovered[0] == recovered[1]
    assert recovered[0]["resolution_id"] == pending["resolution_id"]
    assert recovered[0]["cascade_evidence"]["status"] == "succeeded"
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert first_store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("table", "json_column"),
    [
        ("concern_resolution_cascade_intents", "intent_json"),
        ("concern_resolution_cascade_receipts", "receipt_json"),
    ],
)
def test_cascade_record_tamper_fails_closed_in_get_and_recent(
    tmp_path, table, json_column,
):
    store = ConcernStore(str(tmp_path / f"{table}.db"))
    workspace = WorkspaceEngine(store)
    concern = workspace.bump(
        kind="thread", summary="tamper target", dedup_key=f"tamper-{table}",
    )
    store.resolve_with_owner_record(
        concern.concern_id,
        outcome="done",
        note="terminal without source callback",
        cascade=False,
        resolved_by="owner-test",
    )
    with store._lock:
        row = store._conn.execute(
            f"SELECT rowid,{json_column} FROM {table}"
        ).fetchone()
        payload = json.loads(row[json_column])
        payload["unbound_extra"] = "tampered"
        store._conn.execute(
            f"UPDATE {table} SET {json_column}=? WHERE rowid=?",
            (json.dumps(payload), row["rowid"]),
        )
        store._conn.commit()
    with pytest.raises(ValueError, match="cascade .* integrity check failed"):
        store.get_resolution(concern.concern_id)
    with pytest.raises(ValueError, match="cascade .* integrity check failed"):
        store.recent_resolutions()


def test_two_connections_share_one_base_intent_and_terminal_receipt(tmp_path):
    path = tmp_path / "workspace.db"
    first_store = ConcernStore(str(path))
    second_store = ConcernStore(str(path))
    workspace = WorkspaceEngine(first_store)
    concern = workspace.bump(
        kind="goal",
        summary="cross-connection race",
        dedup_key="cross-connection-race",
        sources=["task:item-1"],
    )

    def _resolve(store):
        return store.resolve_with_owner_record(
            concern.concern_id,
            outcome="done",
            note="one exact cross-connection decision",
            cascade=True,
            resolved_by="owner-test",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved = list(pool.map(_resolve, (first_store, second_store)))
    assert sorted(created for _, created in resolved) == [False, True]
    assert {
        receipt["cascade_evidence"]["status"] for receipt, _ in resolved
    } == {"pending"}
    assert first_store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolutions"
    ).fetchone()[0] == 1
    assert first_store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_intents"
    ).fetchone()[0] == 1

    result = [{"source": "task:item-1", "settled": True}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        terminal = list(pool.map(
            lambda store: store.finalize_owner_cascade(
                concern.concern_id, results=result,
            ),
            (first_store, second_store),
        ))
    assert terminal[0] == terminal[1]
    assert terminal[0]["cascade_evidence"]["status"] == "succeeded"
    assert first_store._conn.execute(
        "SELECT COUNT(*) FROM concern_resolution_cascade_receipts"
    ).fetchone()[0] == 1
