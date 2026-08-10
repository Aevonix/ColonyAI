"""Tests for the unified colony-agent-bridge worker (v0.21.31).

Covers:
- dry-run mode (no network)
- health monitor: sidecar unreachable detection, autonomy stuck detection,
  initiatives-never-executed detection, alert cooldown
- initiative poller: dedup by id and dedup_key, seen-set rotation
- one-shot cycle orchestration
"""

from __future__ import annotations

import json
import hashlib
import hmac
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from colony_sidecar.workers import agent_bridge, queue_worker


def _work_order_params():
    return {
        "schema": "WorkOrderV1",
        "version": 1,
        "source": "project_engine",
        "work_order_id": "work-bridge",
        "work_order_digest": "a" * 64,
        "project_id": "project-1",
        "step_id": "step-1",
        "step_ordinal": 1,
        "objective": "bounded task",
        "success_criteria": ["return evidence"],
        "context_refs": ["memory:one"],
        "capability_allowlist": ["memory:read", "reasoning"],
        "risk_class": "internal",
        "recipient_scope": "owner",
        "max_runtime_seconds": 60,
        "max_attempts": 2,
        "issued_at": "2026-07-12T00:00:00+00:00",
        "deadline": "2026-07-13T00:00:00+00:00",
        "action_hint": "agent_project_analyze",
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "COLONY_URL", "COLONY_API_KEY", "COLONY_INITIATIVE_WEBHOOK",
        "COLONY_JOBS_WEBHOOK_URL", "COLONY_AGENT_NAME",
        "COLONY_WORKER_NODE_ID", "COLONY_WORKER_MAX_JOBS",
        "COLONY_BRIDGE_POLL_SECS", "COLONY_BRIDGE_SKILLS_HOURS",
        "COLONY_BRIDGE_LOG_CHANNEL", "COLONY_BRIDGE_PLATFORM",
        "COLONY_BRIDGE_STATE_DIR", "HERMES_SKILLS_DIR",
        "COLONY_AGENT_WORKER_ROUTES", "COLONY_AGENT_JOB_CLAIMS_ENABLED",
        "COLONY_HERMES_WEBHOOK_SECRET", "COLONY_HERMES_WEBHOOK_V1_COMPAT",
    ):
        monkeypatch.delenv(var, raising=False)


def _no_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(urllib.request, "urlopen", boom)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_dry_run_no_network(monkeypatch, tmp_path, capsys):
    _no_network(monkeypatch)
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert agent_bridge.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "colony_url" in out


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------

def test_health_sidecar_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    monitor = agent_bridge.HealthMonitor(tmp_path)

    with patch.object(agent_bridge, "_get", return_value=None):
        result = monitor.check(cfg)

    assert result["ok"] is False
    assert any(a["type"] == "sidecar_unreachable" for a in result["alerts"])


def test_health_sidecar_unreachable_escalates(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    monitor = agent_bridge.HealthMonitor(tmp_path)

    with patch.object(agent_bridge, "_get", return_value=None):
        r1 = monitor.check(cfg)
    assert r1["alerts"][0]["severity"] == "warning"

    # Force alert cooldown reset so consecutive check fires again
    monitor._state["last_alert_sidecar_unreachable"] = ""
    with patch.object(agent_bridge, "_get", return_value=None):
        r2 = monitor.check(cfg)
    assert r2["alerts"][0]["severity"] == "warning"

    monitor._state["last_alert_sidecar_unreachable"] = ""
    with patch.object(agent_bridge, "_get", return_value=None):
        r3 = monitor.check(cfg)
    assert r3["alerts"][0]["severity"] == "critical"


def test_health_autonomy_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    monitor = agent_bridge.HealthMonitor(tmp_path)
    # Seed a previous autonomy snapshot
    monitor._state["last_autonomy"] = {"ticks": 500, "running": True}

    def fake_get(c, path, timeout=10):
        if "health" in path:
            return {"status": "ok"}
        if "autonomy" in path:
            return {"running": True, "ticks": 500, "initiatives_generated": 10, "actions_executed": 5}
        return None

    with patch.object(agent_bridge, "_get", side_effect=fake_get):
        result = monitor.check(cfg)

    assert result["ok"] is True
    assert any(a["type"] == "autonomy_stuck" for a in result["alerts"])


def test_health_initiatives_never_executed(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    monitor = agent_bridge.HealthMonitor(tmp_path)

    def fake_get(c, path, timeout=10):
        if "health" in path:
            return {"status": "ok"}
        if "autonomy" in path:
            return {"running": True, "ticks": 1127, "initiatives_generated": 6955, "actions_executed": 0}
        return None

    with patch.object(agent_bridge, "_get", side_effect=fake_get):
        result = monitor.check(cfg)

    assert result["ok"] is True
    assert any(a["type"] == "initiatives_never_executed" for a in result["alerts"])


def test_health_reads_status_and_does_not_pass_skipped_probe(tmp_path, monkeypatch):
    """A degraded sidecar must not report ok, an empty 200 health body is a
    failure, and an unreachable autonomy endpoint is a skipped probe, not a
    passed one."""
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    monitor = agent_bridge.HealthMonitor(tmp_path)

    def fake_get(c, path, timeout=10):
        if "health" in path:
            return {"status": "degraded"}
        return None  # autonomy status endpoint down

    with patch.object(agent_bridge, "_get", side_effect=fake_get):
        result = monitor.check(cfg)

    assert result["ok"] is False
    assert result["autonomy_probe"] == "skipped"
    assert any(a["type"] == "sidecar_degraded" for a in result["alerts"])
    assert any(a["type"] == "autonomy_status_unavailable" for a in result["alerts"])

    # An empty 200 body is a failure, not a pass.
    monitor2 = agent_bridge.HealthMonitor(tmp_path / "m2")
    (tmp_path / "m2").mkdir(exist_ok=True)
    with patch.object(agent_bridge, "_get", return_value={}):
        result2 = monitor2.check(cfg)
    assert result2["ok"] is False
    assert any(a["type"] == "sidecar_unreachable" for a in result2["alerts"])


def test_health_alert_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    monitor = agent_bridge.HealthMonitor(tmp_path)

    with patch.object(agent_bridge, "_get", return_value=None):
        r1 = monitor.check(cfg)
    assert len(r1["alerts"]) == 1

    # Second check within cooldown should NOT fire another alert
    with patch.object(agent_bridge, "_get", return_value=None):
        r2 = monitor.check(cfg)
    assert len(r2["alerts"]) == 0


# ---------------------------------------------------------------------------
# InitiativePoller
# ---------------------------------------------------------------------------

def test_poller_fires_pending_initiatives(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    poller = agent_bridge.InitiativePoller(tmp_path)

    fired_payloads = []

    def fake_get(c, path, timeout=10):
        return {"initiatives": [
            {"id": "i1", "status": "pending", "initiative_type": "follow_up", "created_at": "2026-01-01"},
            {"id": "i2", "status": "cancelled", "initiative_type": "relationship"},
            {"id": "i3", "status": "pending", "initiative_type": "commitment", "dedup_key": "dk3", "created_at": "2026-01-01"},
        ]}

    def fake_urlopen(req, timeout=10):
        fired_payloads.append(json.loads(req.data))

        class Resp:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Resp()

    with patch.object(agent_bridge, "_get", side_effect=fake_get), \
         patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        count = poller.poll(cfg)

    assert count == 2
    assert len(fired_payloads) == 2
    types = [p["payload"]["initiative_type"] for p in fired_payloads]
    assert "follow_up" in types
    assert "commitment" in types


def test_poller_dedup_by_id_and_key(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    poller = agent_bridge.InitiativePoller(tmp_path)

    call_count = 0

    def fake_urlopen(req, timeout=10):
        nonlocal call_count
        call_count += 1

        class Resp:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Resp()

    initiatives = [
        {"id": "i1", "status": "pending", "dedup_key": "dk1", "initiative_type": "x", "created_at": ""},
    ]

    with patch.object(agent_bridge, "_get", return_value={"initiatives": initiatives}), \
         patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        poller.poll(cfg)
    assert call_count == 1

    # Same id again
    with patch.object(agent_bridge, "_get", return_value={"initiatives": initiatives}), \
         patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        poller.poll(cfg)
    assert call_count == 1  # not incremented

    # Different id but same dedup_key
    initiatives2 = [
        {"id": "i2", "status": "pending", "dedup_key": "dk1", "initiative_type": "x", "created_at": ""},
    ]
    with patch.object(agent_bridge, "_get", return_value={"initiatives": initiatives2}), \
         patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
        poller.poll(cfg)
    assert call_count == 1  # still not incremented


def test_poller_seen_set_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    poller = agent_bridge.InitiativePoller(tmp_path)
    poller._seen_ids = {f"id-{i}" for i in range(6000)}
    poller._save()
    assert len(poller._seen_ids) == 2000


# ---------------------------------------------------------------------------
# AgentBridge.cycle (one-shot orchestration)
# ---------------------------------------------------------------------------

def test_cycle_runs_all_phases(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    bridge = agent_bridge.AgentBridge(cfg)

    def fake_get(c, path, timeout=10):
        if "health" in path:
            return {"status": "ok"}
        if "autonomy" in path:
            return {"running": True, "ticks": 10, "initiatives_generated": 5, "actions_executed": 3}
        if "initiatives" in path:
            return {"initiatives": []}
        return {}

    with patch.object(agent_bridge, "_get", side_effect=fake_get), \
         patch.object(agent_bridge, "_post", return_value=None):
        result = bridge.cycle()

    assert result["health_ok"] is True
    assert result["initiatives_fired"] == 0
    assert result["jobs_dispatched"] == 0


def test_bridge_starts_claim_before_webhook_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    cfg = agent_bridge._cfg()
    cfg["max_jobs"] = 1
    calls = []

    def fake_post(_cfg, url, body, timeout=10):  # noqa: ARG001
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/jobs/claim"):
            return {"job_id": "job-bridge", "payload": {}}
        return {"success": True}

    monkeypatch.setattr(agent_bridge, "_post", fake_post)
    monkeypatch.setattr(
        agent_bridge.urllib.request,
        "urlopen",
        lambda request, timeout=15: calls.append("webhook") or object(),
    )
    assert agent_bridge.QueueWorker().claim_and_dispatch(cfg) == 1
    assert calls.index("start") < calls.index("webhook")


def test_generic_worker_routes_are_exact_runtime_configuration(
    monkeypatch,
):
    assert queue_worker.agent_action_capabilities("agent_sync")[-1:] == [
        "agent_sync:v1",
    ]
    assert queue_worker.agent_action_capabilities("hermes_run")[-1:] == [
        "hermes_run:v1",
    ]
    with pytest.raises(ValueError):
        queue_worker.agent_action_capabilities("")
    with pytest.raises(ValueError):
        queue_worker.agent_action_capabilities("action_plane")

    captured = []

    def fake_post(_cfg, url, body, timeout=15):  # noqa: ARG001
        captured.append((url, body))
        return None

    monkeypatch.setattr(queue_worker, "_post", fake_post)
    cfg = {
        "colony_url": "http://colony",
        "node_id": "generic-node",
    }
    monkeypatch.setenv("COLONY_AGENT_WORKER_ROUTES", "agent_sync")
    queue_worker.register_worker(cfg)
    queue_worker.claim_job(cfg)
    assert len(captured) == 2
    for _url, body in captured:
        assert "agent_sync:v1" in body["capabilities"]
        assert "hermes_run:v1" not in body["capabilities"]
        assert "action_plane:v1" not in body["capabilities"]
        assert "work_order:v1" not in body["capabilities"]


def test_external_bridge_register_and_claim_use_same_narrow_route(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_AGENT_WORKER_ROUTES", "hermes_run")
    cfg = agent_bridge._cfg()
    bodies = []

    def fake_post(_cfg, url, body, timeout=15):  # noqa: ARG001
        bodies.append((url, body))
        return None

    monkeypatch.setattr(agent_bridge, "_post", fake_post)
    assert agent_bridge.QueueWorker().claim_and_dispatch(cfg) == 0
    assert len(bodies) == 2
    for _url, body in bodies:
        assert "hermes_run:v1" in body["capabilities"]
        assert "agent_sync:v1" not in body["capabilities"]
        assert "action_plane:v1" not in body["capabilities"]


def test_global_claim_kill_switch_stops_external_workers_but_not_initiatives(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_AGENT_JOB_CLAIMS_ENABLED", "false")
    cfg = agent_bridge._cfg()
    bridge = agent_bridge.AgentBridge(cfg)
    calls = []
    monkeypatch.setattr(
        bridge._health, "check", lambda _cfg: {"ok": True, "alerts": []},
    )
    monkeypatch.setattr(
        bridge._poller, "poll",
        lambda _cfg: calls.append("initiatives") or 2,
    )
    monkeypatch.setattr(
        bridge._skills, "sync_if_due", lambda _cfg: 0,
    )
    monkeypatch.setattr(
        agent_bridge, "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("claim path must stay dark")
        ),
    )

    result = bridge.cycle()
    assert result["initiatives_fired"] == 2
    assert result["jobs_dispatched"] == 0
    assert calls == ["initiatives"]


def test_health_alert_delivery_uses_exact_v2_signed_bytes(
    monkeypatch,
):
    monkeypatch.setenv("COLONY_HERMES_WEBHOOK_SECRET", "alert-secret")
    monkeypatch.setattr(queue_worker.time, "time", lambda: 1234567890)
    captured = []

    def fake_urlopen(request, timeout=10):
        captured.append(request)
        return object()

    monkeypatch.setattr(agent_bridge.urllib.request, "urlopen", fake_urlopen)
    agent_bridge.deliver_alerts(
        {"initiative_webhook": "http://agent/alerts"},
        [{
            "type": "queue_stalled",
            "severity": "warning",
            "message": "queue has not advanced",
            "at": "2026-07-12T00:00:00+00:00",
        }],
    )
    assert len(captured) == 1
    request = captured[0]
    assert request.headers["X-webhook-timestamp"] == "1234567890"
    assert request.headers["X-webhook-signature-v2"] == hmac.new(
        b"alert-secret",
        b"1234567890." + request.data,
        hashlib.sha256,
    ).hexdigest()
    assert json.loads(request.data)["type"] == "alert"


def test_both_external_bridge_builders_forward_work_order_contract(
    tmp_path, monkeypatch,
):
    params = _work_order_params()
    cfg = {
        "colony_url": "http://colony",
        "webhook_url": "http://agent/jobs",
        "jobs_webhook": "http://agent/jobs",
        "node_id": "worker",
        "max_jobs": 1,
    }
    pure = queue_worker.build_webhook_payload(cfg, {
        "job_id": "job-1",
        "claim_attempt_id": "attempt-1",
        "payload": params,
    })["payload"]
    assert pure["work_order"]["source"] == "project_engine"
    assert pure["result_contract"]["complete_body_shape"]["output"][
        "execution_result"
    ]["work_order_id"] == "work-bridge"

    captured = []

    def fake_post(_cfg, url, body, timeout=10):  # noqa: ARG001
        if url.endswith("/jobs/claim"):
            return {
                "job_id": "job-1",
                "claim_attempt_id": "attempt-1",
                "payload": params,
            }
        return {"success": True}

    monkeypatch.setattr(agent_bridge, "_post", fake_post)
    monkeypatch.setattr(
        agent_bridge.urllib.request,
        "urlopen",
        lambda request, timeout=15: captured.append(
            json.loads(request.data)
        ) or object(),
    )
    agent_bridge.QueueWorker().claim_and_dispatch(cfg)
    external = captured[0]["payload"]
    assert external["work_order"]["step_ordinal"] == 1
    assert external["result_contract"]["effect_class"] == "none"


def test_hermes_webhook_encoder_uses_pinned_v2_exact_body_hmac(monkeypatch):
    monkeypatch.delenv("COLONY_HERMES_WEBHOOK_V1_COMPAT", raising=False)
    body, headers = queue_worker.encode_hermes_webhook(
        {"type": "agent_job", "payload": {"job_id": "j1"}},
        secret="route-secret",
        timestamp=1234567890,
    )
    assert headers["X-Webhook-Timestamp"] == "1234567890"
    assert headers["X-Webhook-Signature-V2"] == hmac.new(
        b"route-secret", b"1234567890." + body, hashlib.sha256,
    ).hexdigest()
    assert "X-Webhook-Signature" not in headers
    assert headers["X-Request-ID"] == "j1"
    assert json.loads(body)["payload"]["job_id"] == "j1"
