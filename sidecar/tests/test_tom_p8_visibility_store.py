"""Durable, content-bound P8 visibility envelopes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import sqlite3

import pytest

from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactVisibilityV1,
    ViewerContextV1,
    content_digest,
)
from colony_sidecar.tom.visibility_store import (
    FactVisibilityStore,
    VisibilityEnvelopeConflictError,
    open_visibility_envelope_store,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _viewer(person: str, *, attested: bool = True) -> ViewerContextV1:
    return ViewerContextV1(
        principal_id=f"surface:{person}" if person else "",
        viewer_person_id=person,
        owner_person_id="owner",
        audiences=("global",),
        conversation_scope="dm:alice",
        scope_revision="scope:1" if attested else "",
        attested=attested,
    )


def _candidate(
    fact_ref: str,
    text: str,
    *,
    subject: str = "alice",
    viewer_scope: str = "person:alice",
    shareability: str = "subject_private",
    confidence: float = 0.9,
    observed_at: datetime = NOW - timedelta(hours=1),
    fresh_until: datetime = NOW + timedelta(days=1),
) -> FactCandidateV1:
    return FactCandidateV1(
        content=text,
        visibility=FactVisibilityV1(
            fact_ref=fact_ref,
            content_digest=content_digest(text),
            source_ref=f"turn:{fact_ref}",
            subject_person_id=subject,
            viewer_scope=viewer_scope,
            shareability=shareability,
            confidence=confidence,
            observed_at=observed_at.isoformat(),
            fresh_until=fresh_until.isoformat(),
            evidence_refs=(f"turn:{fact_ref}",),
        ),
    )


def test_default_factory_is_dark_and_creates_no_state(tmp_path):
    path = tmp_path / "nested" / "visibility.db"
    assert open_visibility_envelope_store(path) is None
    assert not path.exists()
    assert not path.parent.exists()


def test_append_exact_replay_restart_and_content_is_never_persisted(tmp_path):
    path = tmp_path / "visibility.db"
    candidate = _candidate(
        "fact:alice:preference", "Alice privately prefers concise updates")
    store = FactVisibilityStore(path)
    first = store.append(candidate)
    replay = store.append(candidate)

    assert first.appended is True and first.replayed is False
    assert replay.appended is False and replay.replayed is True
    assert replay.envelope == first.envelope
    assert store.envelope_count() == 1
    store.close()

    reopened = FactVisibilityStore(path)
    assert reopened.get(candidate.visibility.fact_ref) == candidate.visibility
    reopened.close()
    assert os.stat(path).st_mode & 0o777 == 0o600

    connection = sqlite3.connect(path)
    raw = repr(connection.execute(
        "SELECT * FROM fact_visibility_envelopes").fetchall())
    connection.close()
    assert candidate.content not in raw
    assert candidate.visibility.content_digest in raw


def test_fact_ref_replay_with_changed_content_or_scope_is_conflict(tmp_path):
    store = FactVisibilityStore(tmp_path / "visibility.db")
    original = _candidate("fact:stable", "original")
    store.append(original)

    with pytest.raises(VisibilityEnvelopeConflictError, match="immutable"):
        store.append(_candidate("fact:stable", "changed"))
    with pytest.raises(VisibilityEnvelopeConflictError, match="immutable"):
        store.append(_candidate(
            "fact:stable", "original", subject="owner",
            viewer_scope="owner", shareability="owner_private"))
    assert store.envelope_count() == 1


def test_store_rechecks_candidate_content_digest_binding(tmp_path):
    candidate = _candidate("fact:bound", "bound content")
    # Frozen dataclasses prevent normal mutation, but persistence still treats
    # its input as a boundary and rechecks the binding instead of trusting it.
    object.__setattr__(candidate.visibility, "content_digest", "0" * 64)
    with pytest.raises(ValueError, match="content digest"):
        FactVisibilityStore(tmp_path / "visibility.db").append(candidate)


def test_schema_has_append_only_guards_and_required_query_indexes(tmp_path):
    path = tmp_path / "visibility.db"
    store = FactVisibilityStore(path)
    store.append(_candidate("fact:indexed", "indexed"))
    store.close()
    connection = sqlite3.connect(path)
    indexes = {
        row[1]: tuple(column[2] for column in connection.execute(
            f"PRAGMA index_info('{row[1]}')").fetchall())
        for row in connection.execute(
            "PRAGMA index_list('fact_visibility_envelopes')").fetchall()
    }
    assert indexes["idx_fact_visibility_viewer_fresh"] == (
        "viewer_scope", "fresh_until")
    assert indexes["idx_fact_visibility_subject_fresh"] == (
        "subject_person_id", "fresh_until")
    assert indexes["idx_fact_visibility_shareability_fresh"] == (
        "shareability", "fresh_until")
    assert indexes["idx_fact_visibility_fresh"] == ("fresh_until",)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE fact_visibility_envelopes SET confidence=0.1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM fact_visibility_envelopes")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO fact_visibility_envelopes "
            "SELECT * FROM fact_visibility_envelopes")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO fact_visibility_envelopes "
            "SELECT seq,fact_ref||':replacement',visibility_digest||'0',"
            "content_digest,source_ref,subject_person_id,viewer_scope,"
            "shareability,confidence,observed_at,fresh_until,payload_json,"
            "stored_at FROM fact_visibility_envelopes")
    connection.close()


def test_projection_is_exact_fresh_bounded_and_cross_person_closed(tmp_path):
    store = FactVisibilityStore(tmp_path / "visibility.db")
    records = (
        _candidate("fact:alice", "alice only"),
        _candidate(
            "fact:bob", "bob only", subject="bob",
            viewer_scope="person:bob"),
        _candidate(
            "fact:public", "public", subject="owner",
            viewer_scope="public", shareability="public"),
        _candidate(
            "fact:stale", "stale", fresh_until=NOW - timedelta(seconds=1),
            observed_at=NOW - timedelta(days=1)),
    )
    for record in records:
        store.append(record)

    alice = store.project_authorized(_viewer("alice"), now=NOW)
    assert {row.fact_ref for row in alice.envelopes} == {
        "fact:alice", "fact:public"}
    assert "fact:bob" not in repr(alice.public())
    assert "bob only" not in repr(alice.public())

    bob = store.project_authorized(_viewer("bob"), now=NOW)
    assert {row.fact_ref for row in bob.envelopes} == {
        "fact:bob", "fact:public"}
    unattested = store.project_authorized(
        _viewer("", attested=False), now=NOW)
    assert unattested.envelopes == ()
    assert unattested.viewer_attested is False
    assert unattested.truncated is False

    bounded = store.project_authorized(
        _viewer("owner"), now=NOW, max_envelopes=1)
    assert len(bounded.envelopes) == 1
    assert bounded.truncated is True
    with pytest.raises(ValueError, match="bounded projection"):
        store.project_authorized(
            _viewer("alice"), now=NOW, max_envelopes=0)


def test_concurrent_exact_append_commits_one_envelope(tmp_path):
    path = tmp_path / "visibility.db"
    candidate = _candidate("fact:concurrent", "one immutable envelope")
    stores = [FactVisibilityStore(path) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda store: store.append(candidate), stores))
        assert sum(result.appended for result in results) == 1
        assert sum(result.replayed for result in results) == 7
        assert stores[0].envelope_count() == 1
    finally:
        for store in stores:
            store.close()


def test_projection_order_does_not_depend_on_ingestion_order(tmp_path):
    candidates = tuple(
        _candidate(f"fact:stable:{index}", f"stable {index}")
        for index in range(5)
    )
    first = FactVisibilityStore(tmp_path / "first.db")
    second = FactVisibilityStore(tmp_path / "second.db")
    for candidate in candidates:
        first.append(candidate)
    for candidate in reversed(candidates):
        second.append(candidate)
    first_projection = first.project_authorized(
        _viewer("alice"), now=NOW, max_envelopes=3)
    second_projection = second.project_authorized(
        _viewer("alice"), now=NOW, max_envelopes=3)
    assert tuple(row.fact_ref for row in first_projection.envelopes) == (
        "fact:stable:0", "fact:stable:1", "fact:stable:2")
    assert first_projection.envelopes == second_projection.envelopes
    assert first_projection.audit_digest == second_projection.audit_digest
