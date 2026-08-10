"""Non-destructive competence correction and honest benchmark tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

import colony_sidecar.self_model.store as store_mod
from colony_sidecar.self_model.benchmark import (
    BenchmarkStore, SelfhoodBenchmark, week_window,
)
from colony_sidecar.self_model.reconcile import main as reconcile_main
from colony_sidecar.self_model.store import CompetenceStore
from colony_sidecar.self_model.store import SelfModel
from colony_sidecar.self_model.trust import TrustEngine


WEEK = "2026-W26"
START, END = week_window(WEEK)
T0 = (START + timedelta(days=1)).timestamp()


def _manifest(**overrides):
    value = {
        "schema": "colony.competence-reconciliation/v1",
        "created_by": "test-operator",
        "reason": "worker callback was policy-skipped, not completed work",
        "provenance": {
            "source": "task_queue_audit",
            "artifact_sha256": "a" * 64,
        },
    }
    value.update(overrides)
    return value


def _record_at(monkeypatch, store, domain, outcomes):
    monkeypatch.setattr(store_mod.time, "time", lambda: T0)
    for outcome in outcomes:
        store.record(domain, outcome)
    return store.inspect_events(domain, T0 - 1, T0 + 1)


def test_exact_invalidation_is_append_only_and_corrects_aggregate(
        tmp_path, monkeypatch):
    db = tmp_path / "self.db"
    store = CompetenceStore(str(db))
    events = _record_at(monkeypatch, store, "worker:agent_action", ["success"])
    target = events[0]
    manifest = _manifest(event_corrections=[{
        "event_id": target["id"],
        "target_fingerprint": target["fingerprint"],
        "disposition": "invalidate",
    }])

    assert store.plan_reconciliation(manifest)["would_append"] == 1
    applied = store.apply_reconciliation(manifest)
    assert applied["applied"] == 1
    # Idempotent re-application appends nothing.
    assert store.apply_reconciliation(manifest)["applied"] == 0

    raw = store._conn.execute(  # raw evidence remains untouched
        "SELECT outcome FROM competence_events WHERE id=?", (target["id"],)
    ).fetchone()
    assert raw["outcome"] == "success"
    assert store.events("worker:agent_action") == []
    audited = store.inspect_events(
        "worker:agent_action", T0 - 1, T0 + 1)[0]
    assert audited["valid"] is False
    assert audited["recorded_outcome"] == "success"
    assert store.get("worker:agent_action")["n"] == 0
    assert len(store.reconciliation_ledger()) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE competence_reconciliations SET reason='rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM competence_reconciliations")


def test_fingerprint_mismatch_and_manifest_failure_are_atomic(monkeypatch):
    store = CompetenceStore()
    events = _record_at(monkeypatch, store, "worker:x", ["success", "failure"])
    manifest = _manifest(event_corrections=[
        {"event_id": events[0]["id"],
         "target_fingerprint": events[0]["fingerprint"],
         "disposition": "invalidate"},
        {"event_id": events[1]["id"],
         "target_fingerprint": "0" * 64,
         "disposition": "invalidate"},
    ])
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        store.apply_reconciliation(manifest)
    assert store.reconciliation_ledger() == []
    assert len(store.events("worker:x")) == 2
    malformed = _manifest(event_corrections={"event_id": 1})
    with pytest.raises(ValueError, match="list of objects"):
        store.plan_reconciliation(malformed)


def test_replacement_can_only_change_via_explicit_supersession(monkeypatch):
    store = CompetenceStore()
    target = _record_at(monkeypatch, store, "worker:x", ["success"])[0]
    first = _manifest(event_corrections=[{
        "event_id": target["id"],
        "target_fingerprint": target["fingerprint"],
        "disposition": "replace", "replacement_outcome": "failure",
    }])
    first_id = store.apply_reconciliation(first)["reconciliation_ids"][0]
    assert store.events("worker:x")[0]["outcome"] == "failure"
    assert store.get("worker:x")["failure"] == 1

    conflicting = _manifest(event_corrections=[{
        "event_id": target["id"],
        "target_fingerprint": target["fingerprint"],
        "disposition": "replace", "replacement_outcome": "timeout",
    }])
    with pytest.raises(ValueError, match="explicitly supersede"):
        store.apply_reconciliation(conflicting)

    conflicting["event_corrections"][0]["supersedes"] = first_id
    store.apply_reconciliation(conflicting)
    assert store.events("worker:x")[0]["outcome"] == "timeout"
    row = store.get("worker:x")
    assert row["success"] == 0 and row["failure"] == 0
    assert row["timeout"] == 1 and row["reconciled_events"] == 1
    assert len(store.reconciliation_ledger()) == 2


def test_evidence_gap_suppresses_trust_until_append_only_resolution(monkeypatch):
    store = CompetenceStore()
    _record_at(monkeypatch, store, "worker:agent_action", ["success", "failure"])
    gap_manifest = _manifest(evidence_gaps=[{
        "domain": "worker:agent_action",
        "since_ts": T0 - 1, "until_ts": T0 + 1,
    }])
    gap_id = store.apply_reconciliation(gap_manifest)["reconciliation_ids"][0]

    assert store.events("worker:agent_action") == []
    row = store.get("worker:agent_action")
    assert row["evidence_available"] is False
    assert row["success_rate"] is None and row["excluded_events"] == 2
    assert TrustEngine(store).confidence("worker:agent_action") == 0.0

    resolution = _manifest(
        reason="job ledger correlation now proves every event in the window",
        provenance={"source": "signed_reconciliation", "review": "r-17"},
        resolve_gaps=[{"gap_id": gap_id}],
    )
    store.apply_reconciliation(resolution)
    assert len(store.events("worker:agent_action")) == 2
    assert store.get("worker:agent_action")["success_rate"] == 0.5
    assert TrustEngine(store).confidence("worker:agent_action") == 0.5
    assert len(store.reconciliation_ledger()) == 2


def test_evidence_gap_blocks_graduation_but_not_new_violation_breaker(monkeypatch):
    store = CompetenceStore()
    _record_at(monkeypatch, store, "worker:x", ["success"])
    store.apply_reconciliation(_manifest(evidence_gaps=[{
        "domain": "worker:x", "since_ts": T0 - 1, "until_ts": T0 + 1,
    }]))
    trust = TrustEngine(store)
    trust.set_stage("worker:x", "act_first", notify=False)
    model = SelfModel(store, trust=trust)
    monkeypatch.setattr(store_mod.time, "time", lambda: T0 + 10)
    model.record("worker:x", "failure", violation=True)
    assert trust.stage("worker:x") == "ask_first"


async def test_benchmark_hides_stale_rollup_then_recomputes_exact_correction(
        tmp_path, monkeypatch):
    competence = CompetenceStore(str(tmp_path / "self.db"))
    events = _record_at(
        monkeypatch, competence, "worker:agent_action", ["success", "failure"])
    bench = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "bench.db")), competence=competence)
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore

    first = (await bench.compute_week(WEEK))["metrics"]["actions.success"]
    assert first["value"] == 0.5 and first["denominator"] == 2
    assert first["detail"]["metric_definition"] == (
        "colony.actions-success/v2")
    target = next(e for e in events if e["outcome"] == "success")
    competence.apply_reconciliation(_manifest(event_corrections=[{
        "event_id": target["id"],
        "target_fingerprint": target["fingerprint"],
        "disposition": "invalidate",
    }]))

    stale = bench.snapshot()["rollups"][WEEK]["actions.success"]
    assert stale["value"] is None
    assert stale["detail"]["reason"] == (
        "stale_after_competence_reconciliation")

    rebuilt = (await bench.compute_week(WEEK))["metrics"]["actions.success"]
    assert rebuilt["value"] == 0.0
    assert rebuilt["numerator"] == 0 and rebuilt["denominator"] == 1
    assert rebuilt["detail"]["competence_reconciliation_revision"] == 1


async def test_benchmark_marks_ambiguous_action_window_unavailable(
        tmp_path, monkeypatch):
    competence = CompetenceStore()
    _record_at(monkeypatch, competence, "worker:agent_action", ["success"])
    competence.apply_reconciliation(_manifest(evidence_gaps=[{
        "domain": "worker:agent_action",
        "since_ts": START.timestamp(), "until_ts": END.timestamp(),
    }]))
    bench = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "bench.db")), competence=competence)
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore

    metric = (await bench.compute_week(WEEK))["metrics"]["actions.success"]
    assert metric["value"] is None
    assert metric["detail"]["available"] is False
    assert metric["detail"]["reason"] == "competence_evidence_gap"
    assert metric["detail"]["gaps"][0]["domain"] == "worker:agent_action"


async def test_actions_metric_does_not_truncate_at_one_thousand(
        tmp_path, monkeypatch):
    competence = CompetenceStore()
    events = _record_at(
        monkeypatch, competence, "worker:bulk", ["success"] * 1000 + ["failure"])
    assert len(events) == 1001
    bench = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "bench.db")), competence=competence)
    bench._host_attr = staticmethod(lambda name: None)  # type: ignore
    metric = (await bench.compute_week(WEEK))["metrics"]["actions.success"]
    assert metric["denominator"] == 1001
    assert metric["numerator"] == 1000


def test_legacy_schema_migrates_additively_and_new_provenance_roundtrips(
        tmp_path, monkeypatch, capsys):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE competence (
            domain TEXT PRIMARY KEY, success INTEGER DEFAULT 0,
            failure INTEGER DEFAULT 0, timeout INTEGER DEFAULT 0,
            ewma_latency_secs REAL, last_outcome TEXT, last_outcome_at REAL);
        CREATE TABLE competence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
            outcome TEXT NOT NULL, shadow INTEGER DEFAULT 0,
            violation INTEGER DEFAULT 0, ts REAL NOT NULL);
        INSERT INTO competence VALUES ('worker:x', 1, 0, 0, NULL,
                                       'success', 1.0);
        INSERT INTO competence_events
            (domain, outcome, shadow, violation, ts)
            VALUES ('worker:x', 'success', 0, 0, 1.0);
    """)
    conn.commit()
    conn.close()

    assert reconcile_main([
        "inspect", "--db", str(db), "--domain", "worker:x",
        "--since", "0", "--until", "2"]) == 0
    candidate = json.loads(capsys.readouterr().out)["candidates"][0]
    dry_manifest = tmp_path / "legacy-manifest.json"
    dry_manifest.write_text(json.dumps(_manifest(event_corrections=[{
        "event_id": candidate["event_id"],
        "target_fingerprint": candidate["target_fingerprint"],
        "disposition": "invalidate",
    }])), encoding="utf-8")
    assert reconcile_main([
        "apply", "--db", str(db),
        "--manifest", str(dry_manifest)]) == 0
    capsys.readouterr()
    untouched = sqlite3.connect(str(db))
    cols = {r[1] for r in untouched.execute(
        "PRAGMA table_info(competence_events)").fetchall()}
    tables = {r[0] for r in untouched.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    untouched.close()
    assert "source" not in cols
    assert "competence_reconciliations" not in tables

    store = CompetenceStore(str(db))
    legacy = store.inspect_events("worker:x", 0, 2)[0]
    assert legacy["source"] == "legacy_unattributed"
    monkeypatch.setattr(store_mod.time, "time", lambda: 3.0)
    store.record(
        "worker:x", "failure", source="task_queue",
        source_ref="job-17", evidence_status="verified",
        outcome_contract="colony.worker-outcome/v1",
        evidence={"receipt": "sha256:abc"})
    current = store.inspect_events("worker:x", 2, 4)[0]
    assert current["source"] == "task_queue"
    assert current["source_ref"] == "job-17"
    assert current["evidence_status"] == "verified"
    assert current["outcome_contract"] == "colony.worker-outcome/v1"
    assert current["evidence"] == {"receipt": "sha256:abc"}
    assert store.plan_reconciliation(_manifest(event_corrections=[{
        "event_id": current["id"],
        "target_fingerprint": current["fingerprint"],
        "disposition": "invalidate",
    }]))["would_append"] == 1

    # The historical never-raises contract includes malformed provenance.
    store.record("worker:x", "success", evidence={"bad": object()})
    assert len(store.inspect_events("worker:x", 2, 4)) == 1


def test_cli_dry_run_never_mutates_and_commit_requires_backup(
        tmp_path, monkeypatch, capsys):
    db = tmp_path / "self.db"
    store = CompetenceStore(str(db))
    target = _record_at(monkeypatch, store, "worker:x", ["success"])[0]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(event_corrections=[{
        "event_id": target["id"],
        "target_fingerprint": target["fingerprint"],
        "disposition": "invalidate",
    }])), encoding="utf-8")

    assert reconcile_main([
        "apply", "--db", str(db), "--manifest", str(manifest)]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert store._conn.execute(
        "SELECT COUNT(*) FROM competence_reconciliations").fetchone()[0] == 0

    assert reconcile_main([
        "apply", "--db", str(db), "--manifest", str(manifest),
        "--commit"]) == 2
    assert "--backup is required" in capsys.readouterr().err

    backup = tmp_path / "backups" / "self-before.db"
    assert reconcile_main([
        "apply", "--db", str(db), "--manifest", str(manifest),
        "--commit", "--backup", str(backup)]) == 0
    committed = json.loads(capsys.readouterr().out)
    assert backup.is_file()
    assert len(committed["backup_sha256"]) == 64
    assert store._conn.execute(
        "SELECT COUNT(*) FROM competence_reconciliations").fetchone()[0] == 1
