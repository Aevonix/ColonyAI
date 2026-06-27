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
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from colony_sidecar.workers import agent_bridge


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "COLONY_URL", "COLONY_API_KEY", "COLONY_INITIATIVE_WEBHOOK",
        "COLONY_JOBS_WEBHOOK_URL", "COLONY_AGENT_NAME",
        "COLONY_WORKER_NODE_ID", "COLONY_WORKER_MAX_JOBS",
        "COLONY_BRIDGE_POLL_SECS", "COLONY_BRIDGE_SKILLS_HOURS",
        "COLONY_BRIDGE_LOG_CHANNEL", "COLONY_BRIDGE_PLATFORM",
        "COLONY_BRIDGE_STATE_DIR", "HERMES_SKILLS_DIR",
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
