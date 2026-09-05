"""colony_queue_stats returns a fixed count-only projection.

The raw sidecar payload carries a literal "failed" status key and free text
(hold reasons, delivery errors). The projection keeps the counts, drops the
text, and never carries a bare "failed" or "error" key that a tool-result
failure heuristic would misread.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"


def _load_plugin(name: str = "colony_hermes_queue_stats_projection_test"):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _raw():
    return {
        "by_status": {"cancelled": 10, "completed": 11272, "failed": 22},
        "by_type": {"agent_action": 11290, "research": 2, "thought": 12},
        "total_workers": 2, "available_workers": 2, "registered_workers": 2,
        "active_workers": 2, "stale_workers": 0, "worker_heartbeat_ttl_secs": 90,
        "last_user_message_at": "2026-01-01T00:00:00Z",
        "governance": {
            "holds": {"boundary": 1},
            "held_total": 1,
            "governance_held_jobs": [{"job_id": "j1", "reason": "private text"}],
            "outcome_reconciliation": {"pending": 0, "max_attempts": 0,
                                       "last_error": "private error text"},
        },
        "scheduler": {"running": True, "healthy": True, "last_tick_at": "x",
                      "last_error": None, "tick_count": 5},
    }


def test_projection_keeps_counts_and_drops_text():
    module = _load_plugin()
    out = module._bounded_queue_stats(_raw())
    assert out["schema"] == "ColonyQueueStatsProjectionV1"
    assert out["tasks_by_status"] == {
        "status_cancelled": 10, "status_completed": 11272, "status_failed": 22}
    assert out["tasks_by_type"] == {
        "type_agent_action": 11290, "type_research": 2, "type_thought": 12}
    assert out["workers"]["available_workers"] == 2
    assert out["held_total"] == 1
    assert out["holds"] == {"hold_boundary": 1}
    assert out["scheduler"] == {"running": True, "healthy": True}
    text = json.dumps(out, sort_keys=True, separators=(",", ":"))
    assert "private" not in text
    # the two tokens the host failure heuristic scans for
    assert '"failed"' not in text
    assert '"error"' not in text


@pytest.mark.parametrize("mutate", [
    lambda v: v.pop("governance"),
    lambda v: v.pop("scheduler"),
    lambda v: v["by_status"].update({"Bad Key": 1}),
    lambda v: v["by_status"].update({"failed": -1}),
    lambda v: v["by_status"].update({"failed": True}),
    lambda v: v.update({"active_workers": "2"}),
    lambda v: v["scheduler"].update({"running": "yes"}),
])
def test_projection_rejects_malformed_payloads(mutate):
    module = _load_plugin()
    raw = _raw()
    mutate(raw)
    with pytest.raises(RuntimeError):
        module._bounded_queue_stats(raw)


def test_projection_requires_a_mapping():
    module = _load_plugin()
    with pytest.raises(RuntimeError):
        module._bounded_queue_stats(["not", "a", "mapping"])
