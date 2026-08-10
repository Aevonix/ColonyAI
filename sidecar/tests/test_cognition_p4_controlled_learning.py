"""P4: canonical evidence and controlled adaptive-parameter experiments.

These tests intentionally describe the replacement contract rather than the
legacy whole-week experiment heuristic.  Nothing in this suite touches a live
database or a network service.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from colony_sidecar.api.authority import required_scope
from colony_sidecar.contacts.comms import CommsLog
from colony_sidecar.initiatives.approval_authority import ApprovalAuthorityStore
from colony_sidecar.intelligence.learning.feedback_store import (
    FeedbackStore,
    UserCorrection,
)
from colony_sidecar.intelligence.cognition.strategy_adjuster import StrategyAdjuster
from colony_sidecar.intelligence.cognition.gap_detector import (
    Gap,
    GapDetector,
    GapType,
)
from colony_sidecar.intelligence.cognition.types import GapSeverity
from colony_sidecar.intelligence.cognition.performance_index import (
    CognitivePerformanceIndex as LegacyCPI,
    PerformanceIndexComputer,
)
from colony_sidecar.self_model.benchmark import (
    BenchmarkStore,
    MetricDefinition,
    SelfhoodBenchmark,
    cognition_p4_mode,
    legacy_cpi_payload,
    week_window,
)
from colony_sidecar.self_model.experiments import (
    ExperimentApprovalRequired,
    ExperimentEngine,
    ExperimentStore,
)
from colony_sidecar.self_model.params import AdaptiveParamStore


METRIC = MetricDefinition(
    metric="quality.answer_accuracy",
    version="v1",
    direction="higher",
    unit="ratio",
    evidence_query="receipt.type=answer_grade AND receipt.verified=true",
    minimum_samples=6,
    description="Receipt-verified answer accuracy for an assigned exposure.",
)


def _engine(tmp_path, monkeypatch, *, mode="shadow", pregranted=True):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", mode)
    params = AdaptiveParamStore(str(tmp_path / "params.db"))
    params.register("answer.temperature", 0.2, 0.0, 1.0, "test knob")
    bstore = BenchmarkStore(str(tmp_path / "benchmark.db"))
    bstore.register_definition(METRIC)
    benchmark = SelfhoodBenchmark(bstore)
    estore = ExperimentStore(str(tmp_path / "experiments.db"))
    engine = ExperimentEngine(
        estore,
        params=params,
        benchmark=benchmark,
        pregranted_ranges={"answer.temperature": (0.0, 0.8)} if pregranted else {},
    )
    return engine, params, bstore


def _start(engine, **overrides):
    args = {
        "hypothesis": "a slightly higher temperature improves answer accuracy",
        "ref": "answer.temperature",
        "variant": 0.4,
        "metric": METRIC.metric,
        "metric_version": METRIC.version,
        "max_regression": 0.1,
        "window_days": 7,
        "assignment_mode": "cohort",
        "min_control_samples": 3,
        "min_variant_samples": 3,
        "min_total_samples": 6,
        "min_power": 0.05,
        "min_effect": 0.01,
        "owner_negative_limit": 1,
        "source": "thought-job:thought-1",
    }
    args.update(overrides)
    return engine.propose_and_start(**args)


def _record_balanced(engine, exp_id, *, control=0.5, variant=0.9):
    counts = {"control": 0, "variant": 0}
    i = 0
    while min(counts.values()) < 3 and i < 100:
        exposure = engine.assign_exposure(
            exp_id,
            unit_id=f"turn-{i}",
            sample_principal="surface:test",
            source_ref=f"turn:{i}",
        )
        cohort = exposure["cohort"]
        if counts[cohort] < 3:
            engine.record_outcome(
                exp_id,
                exposure_id=exposure["exposure_id"],
                value=control if cohort == "control" else variant,
                sample_principal="verifier:test",
                source_ref=f"grade:{i}",
                receipt_ref=f"receipt:{i}",
            )
            counts[cohort] += 1
        i += 1
    assert counts == {"control": 3, "variant": 3}


def test_p4_flag_is_default_off(monkeypatch):
    monkeypatch.delenv("COLONY_COGNITION_P4_MODE", raising=False)
    assert cognition_p4_mode() == "off"


def test_metric_definitions_are_immutable_and_samples_attested(tmp_path):
    store = BenchmarkStore(str(tmp_path / "benchmark.db"))
    first = store.register_definition(METRIC)
    assert first["definition_hash"]
    assert store.register_definition(METRIC) == first
    with pytest.raises(ValueError, match="immutable"):
        store.register_definition(MetricDefinition(
            **{**METRIC.__dict__, "evidence_query": "anything=true"}
        ))

    assert store.add_evidence_sample(
        METRIC.metric,
        0.75,
        definition_version=METRIC.version,
        sample_principal="verifier:answer-grader",
        source_ref="turn:abc",
        receipt_ref="receipt:abc",
        sample_id="sample-abc",
    )
    # Idempotent retry, not a duplicate sample.
    assert store.add_evidence_sample(
        METRIC.metric,
        0.75,
        definition_version=METRIC.version,
        sample_principal="verifier:answer-grader",
        source_ref="turn:abc",
        receipt_ref="receipt:abc",
        sample_id="sample-abc",
    )
    rows = store.evidence_samples_in(0, datetime.now(timezone.utc).timestamp() + 60)
    assert len(rows) == 1
    assert rows[0]["sample_principal"] == "verifier:answer-grader"
    assert rows[0]["receipt_ref"] == "receipt:abc"
    with pytest.raises(ValueError, match="registered metric definition"):
        store.add_evidence_sample(
            "made.up_metric", 1.0, definition_version="v1",
            sample_principal="verifier:test", source_ref="source:x",
        )


def test_p4_sqlite_migrations_are_additive(tmp_path):
    benchmark_path = tmp_path / "legacy-benchmark.db"
    with sqlite3.connect(benchmark_path) as conn:
        conn.executescript("""
            CREATE TABLE benchmark_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,value REAL NOT NULL,source TEXT NOT NULL,
                ts REAL NOT NULL,meta TEXT);
            CREATE TABLE benchmark_rollups (
                week TEXT NOT NULL,metric TEXT NOT NULL,value REAL,
                numerator REAL,denominator REAL,detail TEXT,
                computed_at REAL NOT NULL,PRIMARY KEY(week,metric));
            INSERT INTO benchmark_samples(metric,value,source,ts)
                VALUES('latency.jobs_p50_secs',1.5,'legacy',1.0);
        """)
    benchmark = BenchmarkStore(str(benchmark_path))
    legacy = benchmark.samples_in(0, 2)
    assert len(legacy) == 1 and legacy[0]["source"] == "legacy"
    assert legacy[0]["definition_version"] is None
    assert benchmark.definition("actions.success", "v2") is not None

    experiment_path = tmp_path / "legacy-experiments.db"
    with sqlite3.connect(experiment_path) as conn:
        conn.executescript("""
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,hypothesis TEXT NOT NULL,kind TEXT NOT NULL,
                ref TEXT NOT NULL,variant REAL NOT NULL,baseline_param REAL,
                metric TEXT NOT NULL,baseline_metric REAL,baseline_week TEXT,
                max_regression REAL NOT NULL,window_days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',created_at REAL NOT NULL,
                started_at REAL,ends_at REAL,decided_at REAL,
                decision_reason TEXT,source TEXT);
            INSERT INTO experiments(
                id,hypothesis,kind,ref,variant,metric,max_regression,
                window_days,status,created_at)
            VALUES('legacy-exp','old','param','old.knob',0.2,
                   'actions.success',0.05,7,'adopted',1.0);
        """)
    experiments = ExperimentStore(str(experiment_path))
    legacy_exp = experiments.get("legacy-exp")
    assert legacy_exp["status"] == "adopted"
    assert legacy_exp["metric_version"] is None
    assert experiments.exposures("legacy-exp") == []


def test_shadow_experiment_uses_only_linked_exposures_and_never_mutates(
    tmp_path, monkeypatch,
):
    engine, params, benchmark = _engine(tmp_path, monkeypatch, mode="shadow")
    exp = _start(engine)
    assert exp["status"] == "running"
    assert exp["execution_mode"] == "shadow"
    assert params.get("answer.temperature") == pytest.approx(0.2)

    # Unrelated benchmark traffic cannot affect the experiment decision.
    benchmark.add_evidence_sample(
        METRIC.metric, 1.0, definition_version=METRIC.version,
        sample_principal="verifier:unrelated", source_ref="other:turn",
        receipt_ref="other:receipt",
    )
    engine.store.update(exp["id"], ends_at=0)
    decided = engine.evaluate()
    assert decided[0]["status"] == "reverted"
    assert decided[0]["causal_status"] == "observed"
    assert "sample" in decided[0]["decision_reason"]
    assert params.get("answer.temperature") == pytest.approx(0.2)


def test_balanced_receipt_linked_shadow_experiment_can_be_supported(
    tmp_path, monkeypatch,
):
    engine, params, _ = _engine(tmp_path, monkeypatch, mode="shadow")
    exp = _start(engine)
    _record_balanced(engine, exp["id"])
    engine.store.update(exp["id"], ends_at=0)
    decided = engine.evaluate()
    assert decided[0]["status"] == "completed"
    assert decided[0]["causal_status"] == "supported"
    evidence = engine.evidence(exp["id"])
    assert len(evidence["exposures"]) >= 6
    assert len(evidence["outcomes"]) == 6
    assert {o["exposure_id"] for o in evidence["outcomes"]} <= {
        x["exposure_id"] for x in evidence["exposures"]
    }
    # Supported shadow evidence is not authority to move the live parameter.
    assert params.get("answer.temperature") == pytest.approx(0.2)


def test_owner_negative_outcome_aborts_and_reverts_immediately(
    tmp_path, monkeypatch,
):
    engine, params, _ = _engine(tmp_path, monkeypatch, mode="shadow")
    exp = _start(engine)
    exposure = engine.assign_exposure(
        exp["id"], unit_id="owner-turn", sample_principal="surface:test",
        source_ref="turn:owner",
    )
    outcome = engine.record_outcome(
        exp["id"], exposure_id=exposure["exposure_id"], value=0.0,
        sample_principal="owner-feedback:test", source_ref="feedback:owner",
        receipt_ref="receipt:owner", owner_reaction="negative",
    )
    assert outcome["owner_reaction"] == "negative"
    ended = engine.store.get(exp["id"])
    assert ended["status"] == "aborted"
    assert "owner-negative" in ended["decision_reason"]
    assert params.get("answer.temperature") == pytest.approx(0.2)
    replay = engine.record_outcome(
        exp["id"], exposure_id=exposure["exposure_id"], value=0.0,
        sample_principal="owner-feedback:test", source_ref="feedback:owner",
        receipt_ref="receipt:owner", owner_reaction="negative",
    )
    assert replay["outcome_id"] == outcome["outcome_id"]


def test_one_receipt_cannot_inflate_multiple_exposures(tmp_path, monkeypatch):
    engine, _, _ = _engine(tmp_path, monkeypatch, mode="shadow")
    exp = _start(engine)
    first = engine.assign_exposure(
        exp["id"], unit_id="turn-a", sample_principal="surface:test",
        source_ref="turn:a")
    first_retry = engine.assign_exposure(
        exp["id"], unit_id="turn-a", sample_principal="surface:test",
        source_ref="turn:a")
    assert first_retry["exposure_id"] == first["exposure_id"]
    second = engine.assign_exposure(
        exp["id"], unit_id="turn-b", sample_principal="surface:test",
        source_ref="turn:b")
    first_outcome = engine.record_outcome(
        exp["id"], exposure_id=first["exposure_id"], value=1.0,
        sample_principal="verifier:test", source_ref="grade:a",
        receipt_ref="receipt:one")
    first_outcome_retry = engine.record_outcome(
        exp["id"], exposure_id=first["exposure_id"], value=1.0,
        sample_principal="verifier:test", source_ref="grade:a",
        receipt_ref="receipt:one")
    assert first_outcome_retry["outcome_id"] == first_outcome["outcome_id"]
    with pytest.raises(ValueError, match="receipt cannot judge multiple"):
        engine.record_outcome(
            exp["id"], exposure_id=second["exposure_id"], value=1.0,
            sample_principal="verifier:test", source_ref="grade:b",
            receipt_ref="receipt:one")


def test_live_mutation_requires_pregrant_or_bounded_owner_approval(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", "live")
    params = AdaptiveParamStore(str(tmp_path / "params.db"))
    params.register("answer.temperature", 0.2, 0.0, 1.0)
    bstore = BenchmarkStore(str(tmp_path / "benchmark.db"))
    bstore.register_definition(METRIC)
    benchmark = SelfhoodBenchmark(bstore)
    approvals = ApprovalAuthorityStore(tmp_path / "approvals.db")
    engine = ExperimentEngine(
        ExperimentStore(str(tmp_path / "experiments.db")),
        params=params,
        benchmark=benchmark,
        approval_authority=approvals,
        pregranted_ranges={},
    )

    with pytest.raises(ExperimentApprovalRequired) as needed:
        _start(engine, assignment_mode="global")
    proposed = needed.value.experiment
    request = approvals.get_request(proposed["approval_request_id"])
    assert request and request["status"] == "pending"
    assert params.get("answer.temperature") == pytest.approx(0.2)

    approvals.decide(
        request["request_id"], decision="approve",
        decision_id="deck:decision-0001",
        expected_action_digest=request["action_digest"],
        decided_by="owner:deck",
        authority_evidence="phone-authenticated-session:test",
    )
    running = engine.start(proposed["id"])
    assert running["status"] == "running"
    assert running["authority_mode"] == "owner_approved"
    assert params.get("answer.temperature") == pytest.approx(0.4)


def test_incomplete_live_start_is_restored_on_engine_recovery(
    tmp_path, monkeypatch,
):
    engine, params, _ = _engine(tmp_path, monkeypatch, mode="live")
    exp = _start(engine, assignment_mode="global")
    assert params.get("answer.temperature") == pytest.approx(0.4)
    # Simulate a process dying after the parameter commit but before the
    # lifecycle transaction advances to running.
    engine.store.update(exp["id"], status="starting", mutation_applied=0)
    params.close()

    recovered_params = AdaptiveParamStore(str(tmp_path / "params.db"))
    recovered_params.register("answer.temperature", 0.2, 0.0, 1.0)
    recovered = ExperimentEngine(
        ExperimentStore(str(tmp_path / "experiments.db")),
        params=recovered_params,
        benchmark=SelfhoodBenchmark(BenchmarkStore(
            str(tmp_path / "benchmark.db"))),
        pregranted_ranges={"answer.temperature": (0.0, 0.8)},
    )
    row = recovered.store.get(exp["id"])
    assert row["status"] == "aborted"
    assert "restored baseline" in row["decision_reason"]
    assert recovered_params.get("answer.temperature") == pytest.approx(0.2)


def test_experiment_engine_is_the_only_p4_parameter_writer(
    tmp_path, monkeypatch,
):
    engine, params, _ = _engine(tmp_path, monkeypatch, mode="live")
    with pytest.raises(PermissionError, match="ExperimentEngine"):
        params.set("answer.temperature", 0.7, source="strategy_adjuster")
    assert params.get("answer.temperature") == pytest.approx(0.2)
    assert engine is not None


@pytest.mark.asyncio
async def test_legacy_strategy_adjuster_emits_a_proposal_not_a_write(
    tmp_path, monkeypatch,
):
    _, params, _ = _engine(tmp_path, monkeypatch, mode="live")
    params.register("recall.min_relevance", 0.1, 0.0, 0.5)
    adjuster = StrategyAdjuster(graph=object(), params=params)
    result = await adjuster._adjust_threshold(threshold=0.3)
    assert result["success"] is False
    assert result["proposal_required"] is True
    assert params.get("recall.min_relevance") == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_legacy_gap_detector_can_only_persist_a_typed_proposal(
    monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", "shadow")

    class Proposer:
        def __init__(self):
            self.calls = []

        def propose(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "exp-proposal-only", "status": "proposed"}

    proposer = Proposer()
    adjuster = StrategyAdjuster(graph=object())
    adjuster.set_experiment_proposer(proposer)
    adjustment = await adjuster.generate(Gap(
        gap_type=GapType.SEMANTIC_MISMATCH,
        severity=GapSeverity.WARNING,
        description="recall evidence declined", component="retrieval",
        evidence={"metric": "recall.fact_coverage/v2"},
    ))
    applied = await adjuster.apply(adjustment)
    assert applied is False
    assert proposer.calls == [{
        "hypothesis": adjustment.hypothesis,
        "ref": "recall.min_relevance",
        "variant": 0.35,
        "metric": "recall.fact_coverage",
        "metric_version": "v2",
        "assignment_mode": "cohort",
        "max_regression": 0.05,
        "window_days": 7,
        "source": "legacy-cpi-gap:semantic_mismatch",
    }]
    assert adjustment.result["details"][0]["proposal_id"] == \
        "exp-proposal-only"


def test_dedicated_benchmark_and_experiment_scopes():
    assert required_scope("GET", "/v1/host/self/benchmark") == \
        "cognition:benchmark-read"
    assert required_scope("POST", "/v1/host/self/benchmark/samples") == \
        "cognition:benchmark-manage"
    assert required_scope("GET", "/v1/host/self/experiments") == \
        "cognition:experiment-read"
    assert required_scope("POST", "/v1/host/self/experiments") == \
        "cognition:experiment-manage"
    assert required_scope(
        "POST", "/v1/host/self/experiments/exp-1/abort"
    ) == "cognition:experiment-manage"


def test_legacy_cpi_missing_dimensions_are_unavailable_not_synthesized(tmp_path):
    computer = PerformanceIndexComputer(graph=object())
    components = {
        "retrieval": computer._compute_retrieval({}),
        "prediction": computer._compute_prediction({}),
        "goal_progress": computer._compute_goal_progress({}),
        "tool_efficiency": computer._compute_tool_efficiency({}),
        "initiative": computer._compute_initiative({}),
        "response_quality": computer._compute_response_quality({}),
    }
    assert all(not component.available for component in components.values())
    cpi = LegacyCPI(
        **components, overall=0.0, computed_at=datetime.now(timezone.utc),
        available_components=[])
    assert GapDetector().detect(cpi) == []

    benchmark = SelfhoodBenchmark(BenchmarkStore(str(tmp_path / "b.db")))
    payload = legacy_cpi_payload(benchmark)
    assert payload["deprecated"] is True
    assert payload["canonical_endpoint"] == "/v1/host/self/benchmark"
    assert "memory" not in payload and "reasoning" not in payload


class _CommitmentCohort:
    def __init__(self, rows):
        self.rows = rows

    def list(self, **kwargs):
        return {"commitments": list(self.rows)}


class _DeliveryEvidence:
    def snapshot(self):
        return [{"domain": "delivery"}]

    def reconciliation_revision(self, **kwargs):
        return 0

    def active_evidence_gaps(self, *args, **kwargs):
        return []

    def events(self, domain, **kwargs):
        return list(self.rows) if domain == "delivery" else []

    def __init__(self, rows):
        self.rows = rows


def test_commitment_metric_uses_one_due_date_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", "live")
    start, end = week_window("2026-W26")
    inside = start + timedelta(days=2)
    rows = [
        {"id": "on-time", "status": "fulfilled",
         "due_at": inside.isoformat(),
         "fulfilled_at": (inside - timedelta(hours=1)).isoformat()},
        {"id": "late", "status": "fulfilled",
         "due_at": inside.isoformat(),
         "fulfilled_at": (inside + timedelta(hours=1)).isoformat()},
        {"id": "open", "status": "pending", "due_at": inside.isoformat(),
         "fulfilled_at": None},
        {"id": "outside", "status": "fulfilled",
         "due_at": (end + timedelta(days=1)).isoformat(),
         "fulfilled_at": end.isoformat()},
        {"id": "cancelled", "status": "cancelled",
         "due_at": inside.isoformat(), "fulfilled_at": None},
    ]
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        commitments=_CommitmentCohort(rows),
    )
    result = benchmark._m_commitments(
        start, end, start.timestamp(), end.timestamp())
    assert result["numerator"] == 1
    assert result["denominator"] == 3
    assert result["detail"]["late"] == 1
    assert result["detail"]["cohort"] == "due_at_in_iso_week"


def test_initiative_acceptance_requires_exact_message_reaction(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", "live")
    start, end = week_window("2026-W26")
    deliveries = _DeliveryEvidence([
        {"id": 1, "ts": start.timestamp() + 100, "outcome": "success",
         "evidence_status": "verified", "source_ref": "delivery:d1",
         "evidence": {"delivery_id": "delivery:d1"}},
        {"id": 2, "ts": start.timestamp() + 200, "outcome": "success",
         "evidence_status": "verified", "source_ref": "delivery:d2",
         "evidence": {"delivery_id": "delivery:d2"}},
    ])
    comms = CommsLog(str(tmp_path / "comms.db"))
    comms.log(
        "owner", direction="in", summary="yes", reaction="accepted",
        reply_to_ref="delivery:d1",
        ts=(start + timedelta(hours=1)).isoformat())
    # This would have counted in v1's any-inbound-within-24h heuristic.
    comms.log(
        "owner", direction="in", summary="unrelated", reaction="accepted",
        reply_to_ref="delivery:someone-else",
        ts=(start + timedelta(hours=2)).isoformat())
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        competence=deliveries, comms=comms, owner_contact_id="owner")
    result = benchmark._m_acceptance(
        start, end, start.timestamp(), end.timestamp())
    assert result["numerator"] == 1
    assert result["denominator"] == 2
    assert result["detail"]["binding"] == "reply_to_ref"


def test_correction_rate_binds_to_receipt_backed_outbound_cohort(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", "live")
    start, end = week_window("2026-W26")
    comms = CommsLog(str(tmp_path / "comms.db"))
    for index in (1, 2):
        comms.log(
            "owner", direction="out", summary=f"answer {index}",
            external_ref=f"response:r{index}", receipt_ref=f"receipt:r{index}",
            ts=(start + timedelta(hours=index)).isoformat())
    # An outbound row without a receipt is intentionally not a denominator.
    comms.log(
        "owner", direction="out", summary="unverified",
        external_ref="response:unverified",
        ts=(start + timedelta(hours=3)).isoformat())
    feedback = FeedbackStore(str(tmp_path / "feedback.db"))
    feedback.record_correction(UserCorrection(
        correction_id="correction-1",
        timestamp=start + timedelta(hours=4),
        original_response="answer 1", correction_text="fix it",
        correction_type="factual", context_hash="response:r1",
        person_id="owner"))
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        comms=comms, corrections=feedback, owner_contact_id="owner")
    result = benchmark._m_corrections(
        start, end, start.timestamp(), end.timestamp())
    assert result["value"] == pytest.approx(0.5)
    assert result["numerator"] == 1
    assert result["denominator"] == 2


@pytest.mark.asyncio
async def test_recall_probe_is_subject_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", "live")

    class Facts:
        def list_facts(self, **kwargs):
            assert kwargs["contact_id"] == "owner"
            return {"facts": [
                {"id": "owner-fact", "contact_id": "owner",
                 "shareability": "owner_private",
                 "fact": "the owner project uses a cobalt release marker"},
                {"id": "guest-fact", "contact_id": "guest",
                 "shareability": "shared",
                 "fact": "guest unrelated private phrase"},
            ]}

    class Graph:
        def __init__(self):
            self.person_ids = []

        async def recall(self, query, **kwargs):
            self.person_ids.append(kwargs.get("person_id"))
            return [{"content": query}]

    graph = Graph()
    benchmark = SelfhoodBenchmark(
        BenchmarkStore(str(tmp_path / "benchmark.db")),
        graph=graph, facts=Facts(), owner_contact_id="owner", probes=10)
    result = await benchmark._m_recall(0, float("inf"))
    assert result["value"] == 1.0
    assert result["denominator"] == 1
    assert graph.person_ids == ["owner"]
