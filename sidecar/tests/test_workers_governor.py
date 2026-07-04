"""Server-side worker enforcement: the WorkerGovernor (Phase B item 5).

Never trust the worker: capability coverage and owner boundaries are
re-decided server-side at claim time, and the report is audited at completion
(a mutation on a read-only job is a violation). Shadow observes without
blocking; live enforces; each job type is its own trust domain.
"""

from __future__ import annotations

import pytest

from colony_sidecar.directives import Verdict
from colony_sidecar.self_model import (
    ActionJournal, CompetenceStore, SelfModel, TrustEngine,
)
from colony_sidecar.task_queue.governor import WorkerGovernor
from colony_sidecar.task_queue.models import (
    Job, JobCapabilityRequirement, JobType,
)
from colony_sidecar.workers import colony_worker as cw


def _self_model():
    store = CompetenceStore()
    journal = ActionJournal()
    trust = TrustEngine(store, journal=journal)
    return SelfModel(store, trust=trust, journal=journal)


class _FakeDirectives:
    """Minimal DirectiveManager stand-in: returns a fixed verdict."""

    def __init__(self, allowed: bool, reason: str = "ok") -> None:
        self._v = Verdict(allowed=allowed, reason=reason)

    def check(self, action):  # noqa: ARG002
        return self._v


def _read_job(**payload):
    p = {"risk": "read_only", "description": "summarize the changelog"}
    p.update(payload)
    return Job(job_type=JobType.RESEARCH, payload=p,
               capabilities=[JobCapabilityRequirement(name="research")])


def _mutating_job(**payload):
    p = {"risk": "high", "description": "refactor module X"}
    p.update(payload)
    return Job(job_type=JobType.AGENT_ACTION, payload=p,
               tags={"approved_by": "owner"})


# -- claim gate: capability coverage (server-side) -----------------------

def test_live_refuses_worker_lacking_required_capability(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(self_model=_self_model())
    job = _read_job()  # requires "research"
    v = gov.evaluate_claim(job, worker_capabilities={"analyst"}, worker_node_id="w1")
    assert v["allowed"] is False
    assert v["capability_ok"] is False
    assert "research" in v["missing_capabilities"]


def test_live_allows_worker_that_covers_capabilities(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(self_model=_self_model())
    job = _read_job()
    v = gov.evaluate_claim(job, worker_capabilities={"research", "read"})
    assert v["allowed"] is True and v["capability_ok"] is True


def test_required_capability_via_tag_enforced(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(self_model=_self_model())
    job = _read_job()
    job.tags["required_capability"] = "gpu"
    v = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert v["allowed"] is False and "gpu" in v["missing_capabilities"]


# -- claim gate: boundary re-check ---------------------------------------

def test_live_refuses_boundaried_job(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(directive_manager=_FakeDirectives(False, "leave X alone"),
                         self_model=_self_model())
    job = _read_job(description="analyze X")
    v = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert v["allowed"] is False and v["boundary_ok"] is False


def test_live_allows_when_boundary_clear(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(directive_manager=_FakeDirectives(True),
                         self_model=_self_model())
    job = _read_job()
    v = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert v["allowed"] is True and v["boundary_ok"] is True


# -- mode semantics -------------------------------------------------------

def test_shadow_observes_but_never_blocks(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    gov = WorkerGovernor(directive_manager=_FakeDirectives(False, "boundaried"),
                         self_model=_self_model())
    job = _read_job()
    # Missing cap AND boundaried -> would refuse, but shadow allows (calibration).
    v = gov.evaluate_claim(job, worker_capabilities=set())
    assert v["allowed"] is True
    assert v["would_refuse"] is True
    assert v["shadow"] is True and v["enforced"] is False


def test_off_disables_governor(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    gov = WorkerGovernor(self_model=_self_model())
    v = gov.evaluate_claim(_read_job(), worker_capabilities=set())
    assert v["allowed"] is True and v["enforced"] is False
    assert v["reason"] == "governor_off"


# -- completion audit: never trust the report ----------------------------

def test_audit_flags_mutation_on_read_only_job():
    gov = WorkerGovernor()
    job = _read_job()
    audit = gov.audit_report(job, {"summary": "done", "operations": ["commit"],
                                   "commits": 2})
    assert audit["verdict"] == "violation"
    assert audit["findings"]


def test_audit_clean_on_read_only_read_report():
    gov = WorkerGovernor()
    job = _read_job()
    audit = gov.audit_report(job, {"summary": "analysis complete",
                                   "operations": ["analyze", "read"],
                                   "commits": 0})
    assert audit["verdict"] == "clean"


def test_audit_allows_mutation_on_authorized_job():
    gov = WorkerGovernor()
    job = _mutating_job()
    audit = gov.audit_report(job, {"summary": "patched", "operations": ["commit"],
                                   "commits": 1, "branch": "colony/fix"})
    assert audit["verdict"] == "clean"


def test_audit_force_push_always_violation():
    gov = WorkerGovernor()
    job = _mutating_job()
    audit = gov.audit_report(job, {"summary": "x", "force_push": True})
    assert audit["verdict"] == "violation"


def test_audit_empty_report_is_unverified():
    gov = WorkerGovernor()
    assert gov.audit_report(_read_job(), {})["verdict"] == "unverified"


# -- outcome recording feeds the trust engine ----------------------------

@pytest.mark.asyncio
async def test_record_outcome_live_feeds_real_trust_domain(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    job = _read_job()
    await gov.record_outcome(job, {"summary": "ok", "confidence": 0.9},
                             "clean", latency=1.0)
    events = sm.store.events("worker:research", include_shadow=False)
    assert len(events) == 1 and events[0]["outcome"] == "success"
    assert events[0]["shadow"] == 0
    # journaled
    assert sm.journal.recent(domain="worker:research")


@pytest.mark.asyncio
async def test_record_outcome_shadow_is_calibration(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    await gov.record_outcome(_read_job(), {"summary": "ok"}, "clean")
    real = sm.store.events("worker:research", include_shadow=False)
    allev = sm.store.events("worker:research", include_shadow=True)
    assert len(real) == 0 and len(allev) == 1  # shadow event only


@pytest.mark.asyncio
async def test_violation_records_and_trips_breaker(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    # Pre-graduate the domain to act_first so a violation can demote it.
    sm.trust.set_stage("worker:research", "act_first", notify=False)
    gov = WorkerGovernor(self_model=sm)
    job = _read_job()
    await gov.record_outcome(job, {"operations": ["commit"], "commits": 1},
                             "violation")
    ev = sm.store.events("worker:research", include_shadow=False)
    assert ev and ev[0]["violation"] == 1
    assert sm.trust.stage("worker:research") == "ask_first"  # breaker demoted


# -- status ---------------------------------------------------------------

def test_status_reports_mode_and_worker_domains(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    sm.trust.set_stage("worker:research", "ask_first", notify=False)
    gov = WorkerGovernor(self_model=sm)
    st = gov.status()
    assert st["mode"] == "live"
    assert any(d["domain"] == "worker:research" for d in st["worker_domains"])


# -- worker daemon pure helpers ------------------------------------------

def test_worker_parse_report_enforces_read_only_posture():
    report = cw._parse_report(
        '{"summary": "found it", "operations": ["analyze", "commit", "delete"],'
        ' "commits": 5, "confidence": 1.7}')
    assert report["operations"] == ["analyze"]  # mutate ops dropped
    assert report["files_touched"] == [] and report["commits"] == 0
    assert report["confidence"] == 1.0  # clamped


def test_worker_build_messages_includes_job_fields():
    msgs = cw.build_llm_messages({"payload": {"description": "check the logs",
                                              "domain": "ops"}})
    assert msgs[0]["role"] == "system"
    assert "check the logs" in msgs[1]["content"]


def test_worker_config_defaults(monkeypatch):
    for k in ("COLONY_WORKER_CAPABILITIES", "COLONY_WORKER_JOB_TYPES",
              "COLONY_WORKER_NODE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("COLONY_AGENT_NAME", "Test Bot")
    cfg = cw.load_config()
    assert cfg["node_id"] == "test-bot-worker"
    assert "research" in cfg["capabilities"]
