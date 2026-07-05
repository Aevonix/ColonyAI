"""COLONY_AUTONOMY_PRESET: one knob for the autonomy posture.

Resolution contract: explicit env var > preset default > built-in fallback.
The sandbox never goes live from a preset.
"""

import pytest

from colony_sidecar.util import autonomy_preset as ap

_MANAGED = [
    "COLONY_AUTONOMY_PRESET",
    "COLONY_EXECUTOR_ENABLED", "COLONY_COGNITION_ENABLED",
    "COLONY_INTROSPECT_ENABLED", "COLONY_THINKING_MODE",
    "COLONY_PROJECTS_MODE", "COLONY_BELIEFS_MODE",
    "COLONY_WORLD_POPULATE_MODE", "COLONY_WORLD_LLM_EXTRACT",
    "COLONY_SKILLS_DISTILL", "COLONY_ESCALATION_MINING",
    "COLONY_CONNECTORS_MODE", "COLONY_WORKERS_MODE",
    "COLONY_DIRECTED_MODE", "COLONY_SANDBOX_MODE",
    "COLONY_ENABLE_INTERNAL_THINKING",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _MANAGED:
        monkeypatch.delenv(name, raising=False)


class TestResolution:
    def test_fallback_when_nothing_set(self):
        assert ap.resolve("COLONY_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "shadow"
        assert ap.resolve_bool("COLONY_EXECUTOR_ENABLED", False) is False

    def test_preset_fills_unset(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        assert ap.resolve("COLONY_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "shadow"
        assert ap.resolve("COLONY_CONNECTORS_MODE",
                          ("off", "shadow", "live"), "off") == "shadow"
        assert ap.resolve_bool("COLONY_EXECUTOR_ENABLED", False) is True

    def test_explicit_env_beats_preset(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("COLONY_PROJECTS_MODE", "off")
        assert ap.resolve("COLONY_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "off"

    def test_unknown_preset_ignored(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "yolo")
        assert ap.preset_name() == ""
        assert ap.resolve("COLONY_CONNECTORS_MODE",
                          ("off", "shadow", "live"), "off") == "off"

    def test_invalid_explicit_value_falls_back_like_legacy(self, monkeypatch):
        monkeypatch.setenv("COLONY_PROJECTS_MODE", "banana")
        assert ap.resolve("COLONY_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "shadow"

    def test_sandbox_never_live_from_preset(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        assert ap.resolve("COLONY_SANDBOX_MODE",
                          ("off", "dry_run", "live"), "off") == "dry_run"
        # Explicit env is the only path to live.
        monkeypatch.setenv("COLONY_SANDBOX_MODE", "live")
        assert ap.resolve("COLONY_SANDBOX_MODE",
                          ("off", "dry_run", "live"), "off") == "live"

    def test_passive_turns_everything_off(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        snap = ap.snapshot()
        for k, v in snap.items():
            if k in ("preset",):
                continue
            assert v in ("off", "false"), f"{k}={v} not passive"


class TestSubsystemReadersHonorPreset:
    """The actual mode functions each subsystem exposes resolve through
    the preset (this is what makes the knob real, not decorative)."""

    def test_projects(self, monkeypatch):
        from colony_sidecar.projects.models import projects_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        assert projects_mode() == "live"
        monkeypatch.setenv("COLONY_PROJECTS_MODE", "shadow")
        assert projects_mode() == "shadow"

    def test_beliefs(self, monkeypatch):
        from colony_sidecar.beliefs.models import beliefs_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        assert beliefs_mode() == "off"

    def test_workers(self, monkeypatch):
        from colony_sidecar.task_queue.governor import workers_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        assert workers_mode() == "live"

    def test_connectors(self, monkeypatch):
        from colony_sidecar.connectors.manager import connectors_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        assert connectors_mode() == "shadow"

    def test_directed(self, monkeypatch):
        from colony_sidecar.directed.service import directed_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        assert directed_mode() == "dry_run"

    def test_sandbox(self, monkeypatch):
        from colony_sidecar.sandbox.manager import sandbox_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        assert sandbox_mode() == "dry_run"

    def test_mining(self, monkeypatch):
        from colony_sidecar.mining.models import mining_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        assert mining_mode() == "off"

    def test_skills_distill(self, monkeypatch):
        from colony_sidecar.skills_memory.store import skills_distill_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        assert skills_distill_mode() == "live"

    def test_world_populate(self, monkeypatch):
        from colony_sidecar.world_model.populator import populate_mode
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        assert populate_mode() == "off"

    def test_introspection_and_cognition(self, monkeypatch):
        from colony_sidecar.cognition.introspection import introspect_enabled
        from colony_sidecar.cognition.trigger import _cognition_enabled
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        assert introspect_enabled() is True
        assert _cognition_enabled() is True
