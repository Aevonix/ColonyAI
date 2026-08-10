"""P4 startup and HTTP integration regression locks.

These tests exercise the wiring intentionally omitted from the isolated P4
source slice.  They use temporary state and scoped credentials only.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.self_model.params import (
    AdaptiveParamStore,
    register_core_params,
)
from colony_sidecar.server import (
    _initialize_controlled_learning,
    _wire_controlled_learning_pipeline,
)


HOST_GLOBALS = (
    "_adaptive_params",
    "_benchmark",
    "_experiments",
    "_learning_feedback_store",
    "_learner",
    "_metalearner",
)


@pytest.fixture(autouse=True)
def _restore_host_globals():
    originals = {name: getattr(host, name) for name in HOST_GLOBALS}
    yield
    for name, value in originals.items():
        setattr(host, name, value)


def _configure(monkeypatch, state_dir, *, mode="shadow"):
    monkeypatch.setenv("COLONY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("COLONY_COGNITION_P4_MODE", mode)
    monkeypatch.setenv("COLONY_BENCHMARK_ENABLED", "true")
    monkeypatch.setenv("COLONY_EXPERIMENTS_ENABLED", "true")
    monkeypatch.setenv("COLONY_EXPERIMENT_PREGRANTS_JSON", "")
    monkeypatch.setenv("COLONY_SKIP_DOTENV", "1")


def _params(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    params = AdaptiveParamStore(str(state_dir / "colony-params.db"))
    register_core_params(params)
    host.set_adaptive_params(params)
    return params


def _principal(principal, secret, scopes):
    return {
        "principal": principal,
        "status": "active",
        "scopes": scopes,
        "viewer_person_id": "contact-owner",
        "audiences": ["viewer", "owner"],
        "credentials": [
            {"id": "current", "secret": secret, "status": "active"}
        ],
    }


def _app(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    keyring = tmp_path / "api-keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [
            _principal(
                "p4-manager",
                "manager-secret",
                [
                    "api:access",
                    "cognition:benchmark-manage",
                    "cognition:experiment-manage",
                ],
            ),
            _principal(
                "p4-reader",
                "reader-secret",
                ["cognition:benchmark-read", "cognition:experiment-read"],
            ),
        ],
    }))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    app.include_router(host.router)
    return app


def _headers(kind="manager"):
    return {
        "Authorization": f"Bearer {kind}-secret",
        "X-Colony-Principal": f"p4-{kind}",
    }


def _proposal(**overrides):
    payload = {
        "hypothesis": "bounded recall tuning improves verified coverage",
        "ref": "recall.min_relevance",
        "variant": 0.2,
        "metric": "recall.fact_coverage",
        "metric_version": "v2",
        "assignment_mode": "cohort",
        "min_control_samples": 1,
        "min_variant_samples": 1,
        "min_total_samples": 2,
        "min_power": 0.0,
        "min_effect": 0.0,
        "source": "body-spoofed-principal",
        "sample_principal": "body-spoofed-principal",
    }
    payload.update(overrides)
    return payload


def test_startup_uses_one_feedback_store_shared_authority_and_pipeline(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir)
    monkeypatch.setenv(
        "COLONY_EXPERIMENT_PREGRANTS_JSON",
        '{"recall.min_relevance":[0.0,0.25]}',
    )
    params = _params(state_dir)

    wiring = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)

    assert host._learning_feedback_store is wiring["corrections"]
    assert host._benchmark is wiring["benchmark"]
    assert host._experiments is wiring["experiments"]
    assert wiring["benchmark"]._deps["corrections"] is wiring["corrections"]
    assert wiring["experiments"]._benchmark is wiring["benchmark"]
    assert wiring["experiments"]._approval_authority is \
        wiring["approval_authority"]
    assert wiring["approval_authority"].path == \
        state_dir / "approval_authority.db"
    assert wiring["experiments"]._pregrants == {
        "recall.min_relevance": (0.0, 0.25)}

    class Recorder:
        def __init__(self):
            self.value = None

        def set_feedback_store(self, value):
            self.value = value

        def set_experiment_proposer(self, value):
            self.value = value

    pipeline = type("Pipeline", (), {})()
    pipeline.meta_learner = Recorder()
    pipeline.strategy_adjuster = Recorder()
    _wire_controlled_learning_pipeline(pipeline, wiring)
    assert pipeline.meta_learner.value is wiring["corrections"]
    assert pipeline.strategy_adjuster.value is wiring["experiments"]


def test_disabled_benchmark_and_experiments_create_no_feature_artifacts(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir)
    monkeypatch.setenv("COLONY_BENCHMARK_ENABLED", "false")
    monkeypatch.setenv("COLONY_EXPERIMENTS_ENABLED", "false")
    params = _params(state_dir)

    wiring = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)

    # Corrections remain a durable learning input independent of benchmarking.
    assert wiring["corrections"] is host._learning_feedback_store
    assert (state_dir / "colony-learning-feedback.db").exists()
    assert wiring["benchmark"] is None
    assert wiring["experiments"] is None
    assert not (state_dir / "colony-benchmark.db").exists()
    assert not (state_dir / "colony-experiments.db").exists()
    assert not (state_dir / "approval_authority.db").exists()


@pytest.mark.asyncio
async def test_correction_is_persisted_before_continuous_learning(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir)
    params = _params(state_dir)
    wiring = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)

    class Learner:
        def __init__(self):
            self.corrections = []

        async def ingest_correction(self, correction):
            self.corrections.append(correction)

    learner = Learner()
    host.set_learner(learner)
    app = _app(tmp_path)
    payload = {
        "identity": {"host_id": "test"},
        "context": {"session_id": "s1", "contact_id": "contact-owner"},
        "original": "The release is Tuesday",
        "correction": "The release is Wednesday",
        "correction_type": "factual",
        "external_ref": "response:owner:42",
        "correction_id": "correction-owner-42",
        "person_id": "body-spoofed-principal",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/host/learning/correction", headers=_headers(), json=payload)
        replay = await client.post(
            "/v1/host/learning/correction", headers=_headers(), json=payload)

    assert first.status_code == 200 and first.json()["accepted"] is True
    assert replay.status_code == 200 and replay.json()["accepted"] is True
    assert wiring["corrections"].count() == 1
    stored = wiring["corrections"].between(
        "1970-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
    assert stored[0]["context_hash"] == "response:owner:42"
    assert stored[0]["person_id"] == "contact-owner"
    assert learner.corrections[0].context_hash == "response:owner:42"


@pytest.mark.asyncio
async def test_scoped_shadow_routes_derive_principal_and_replay_after_restart(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir, mode="shadow")
    params = _params(state_dir)
    baseline = params.get("recall.min_relevance")
    wiring = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)
    app = _app(tmp_path)

    sample = {
        "metric": "recall.fact_coverage",
        "value": 0.75,
        "definition_version": "v2",
        "source_ref": "verifier:recall:1",
        "receipt_ref": "receipt:recall:1",
        "sample_id": "sample-recall-1",
        "effect_claim": True,
        "sample_principal": "body-spoofed-principal",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            "/v1/host/self/experiments",
            headers=_headers("reader"),
            json=_proposal(),
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["required_scope"] == \
            "cognition:experiment-manage"

        sampled = await client.post(
            "/v1/host/self/benchmark/samples",
            headers=_headers(),
            json={"source": "body-spoofed-principal", "samples": [sample]},
        )
        assert sampled.status_code == 200
        assert sampled.json()["accepted"] == 1

        proposed = await client.post(
            "/v1/host/self/experiments",
            headers=_headers(),
            json=_proposal(),
        )
        assert proposed.status_code == 200
        experiment = proposed.json()["experiment"]
        assert experiment["status"] == "running"
        assert experiment["source"] == "p4-manager"
        exp_id = experiment["id"]
        assert params.get("recall.min_relevance") == pytest.approx(baseline)

        exposure_request = {
            "unit_id": "turn-1",
            "source_ref": "turn:1",
            "sample_principal": "body-spoofed-principal",
        }
        exposed = await client.post(
            f"/v1/host/self/experiments/{exp_id}/exposures",
            headers=_headers(),
            json=exposure_request,
        )
        assert exposed.status_code == 200
        exposure = exposed.json()["exposure"]
        assert exposure["sample_principal"] == "p4-manager"

        outcome_request = {
            "exposure_id": exposure["exposure_id"],
            "value": 0.9,
            "source_ref": "grade:1",
            "receipt_ref": "receipt:grade:1",
            "sample_principal": "body-spoofed-principal",
        }
        outcome = await client.post(
            f"/v1/host/self/experiments/{exp_id}/outcomes",
            headers=_headers(),
            json=outcome_request,
        )
        assert outcome.status_code == 200
        first_outcome_id = outcome.json()["outcome"]["outcome_id"]
        assert outcome.json()["outcome"]["sample_principal"] == "p4-manager"

    rows = wiring["benchmark"].store.evidence_samples_in(0, float("inf"))
    assert len(rows) == 1
    assert rows[0]["sample_principal"] == "p4-manager"

    # A process restart reconstructs every object from the same durable files.
    params.close()
    restarted_params = _params(state_dir)
    restarted = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=restarted_params)
    restarted_app = _app(tmp_path / "restart")
    async with AsyncClient(
        transport=ASGITransport(app=restarted_app), base_url="http://test"
    ) as client:
        sample_retry = await client.post(
            "/v1/host/self/benchmark/samples",
            headers=_headers(),
            json={"source": "new-spoof", "samples": [sample]},
        )
        exposure_retry = await client.post(
            f"/v1/host/self/experiments/{exp_id}/exposures",
            headers=_headers(),
            json=exposure_request,
        )
        outcome_retry = await client.post(
            f"/v1/host/self/experiments/{exp_id}/outcomes",
            headers=_headers(),
            json=outcome_request,
        )
        evidence = await client.get(
            f"/v1/host/self/experiments/{exp_id}/evidence",
            headers=_headers("reader"),
        )

    assert sample_retry.json()["accepted"] == 1
    assert len(restarted["benchmark"].store.evidence_samples_in(
        0, float("inf"))) == 1
    assert exposure_retry.json()["exposure"]["exposure_id"] == \
        exposure["exposure_id"]
    assert outcome_retry.json()["outcome"]["outcome_id"] == first_outcome_id
    assert evidence.status_code == 200
    assert len(evidence.json()["exposures"]) == 1
    assert len(evidence.json()["outcomes"]) == 1
    assert restarted_params.get("recall.min_relevance") == pytest.approx(
        baseline)


@pytest.mark.asyncio
async def test_live_proposal_returns_202_then_starts_after_exact_approval(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir, mode="live")
    params = _params(state_dir)
    baseline = params.get("recall.min_relevance")
    wiring = _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)
    app = _app(tmp_path)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = await client.post(
            "/v1/host/self/experiments",
            headers=_headers(),
            json=_proposal(assignment_mode="global"),
        )
        assert pending.status_code == 202
        body = pending.json()
        assert body["status"] == "approval_required"
        exp = body["experiment"]
        assert params.get("recall.min_relevance") == pytest.approx(baseline)

        request_row = wiring["approval_authority"].get_request(
            body["approval_request_id"])
        assert request_row["status"] == "pending"
        wiring["approval_authority"].decide(
            request_row["request_id"],
            decision="approve",
            decision_id="deck:decision-0001",
            expected_action_digest=request_row["action_digest"],
            decided_by="owner:deck",
            authority_evidence="phone-authenticated-session:test",
        )

        started = await client.post(
            f"/v1/host/self/experiments/{exp['id']}/start",
            headers=_headers(),
        )
        started_retry = await client.post(
            f"/v1/host/self/experiments/{exp['id']}/start",
            headers=_headers(),
        )

    assert started.status_code == 200
    assert started.json()["experiment"]["authority_mode"] == "owner_approved"
    assert started_retry.status_code == 200
    assert started_retry.json()["idempotent_replay"] is True
    assert params.get("recall.min_relevance") == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_cpi_and_cycle_publish_truthful_legacy_payload(
    tmp_path, monkeypatch,
):
    state_dir = tmp_path / "state"
    _configure(monkeypatch, state_dir)
    params = _params(state_dir)
    _initialize_controlled_learning(
        state_dir=state_dir, adaptive_params=params)
    host.set_metalearner(None)
    app = _app(tmp_path)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        cpi = await client.get(
            "/v1/host/cognition/cpi", headers=_headers("reader"))
        cycle = await client.post(
            "/v1/host/cognition/cycle",
            headers=_headers(),
            json={"identity": {"host_id": "test"}},
        )

    assert cpi.status_code == 200
    assert cpi.json()["deprecated"] is True
    assert cpi.json()["available"] is True
    assert "memory" not in cpi.json()
    assert cycle.status_code == 200
    assert cycle.json()["cpi"]["canonical"] == "selfhood_benchmark"
    assert "reasoning" not in cycle.json()["cpi"]
