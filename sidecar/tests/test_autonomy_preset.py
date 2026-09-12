"""PACOMIND_AUTONOMY_PRESET: one knob for the autonomy posture.

Resolution contract: explicit env var > preset default > built-in fallback.
The sandbox never goes live from a preset.
"""

import pytest

from pacomind.util import autonomy_preset as ap

_MANAGED = [
    "PACOMIND_AUTONOMY_PRESET",
    "PACOMIND_EXECUTOR_ENABLED", "PACOMIND_COGNITION_ENABLED",
    "PACOMIND_INTROSPECT_ENABLED", "PACOMIND_THINKING_MODE",
    "PACOMIND_PROJECTS_MODE", "PACOMIND_BELIEFS_MODE",
    "PACOMIND_WORLD_POPULATE_MODE", "PACOMIND_WORLD_LLM_EXTRACT",
    "PACOMIND_SKILLS_DISTILL", "PACOMIND_ESCALATION_MINING",
    "PACOMIND_CONNECTORS_MODE", "PACOMIND_WORKERS_MODE",
    "PACOMIND_DIRECTED_MODE", "PACOMIND_SANDBOX_MODE",
    "PACOMIND_ENABLE_INTERNAL_THINKING",
    "PACOMIND_EXPECTATIONS", "PACOMIND_WORKSPACE",
    "PACOMIND_AUTONOMY_MODE", "PACOMIND_PRESET_LOOP_COUPLING",
    "PACOMIND_AUTONOMY_TICK_INTERVAL_SECS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _MANAGED:
        monkeypatch.delenv(name, raising=False)


class TestResolution:
    def test_fallback_when_nothing_set(self):
        assert ap.resolve("PACOMIND_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "shadow"
        assert ap.resolve_bool("PACOMIND_EXECUTOR_ENABLED", False) is False

    def test_preset_fills_unset(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "calibration")
        assert ap.resolve("PACOMIND_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "shadow"
        assert ap.resolve("PACOMIND_CONNECTORS_MODE",
                          ("off", "shadow", "live"), "off") == "shadow"
        assert ap.resolve_bool("PACOMIND_EXECUTOR_ENABLED", False) is True

    def test_explicit_env_beats_preset(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("PACOMIND_PROJECTS_MODE", "off")
        assert ap.resolve("PACOMIND_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "off"

    def test_unknown_preset_ignored(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "yolo")
        assert ap.preset_name() == ""
        assert ap.resolve("PACOMIND_CONNECTORS_MODE",
                          ("off", "shadow", "live"), "off") == "off"

    def test_invalid_explicit_value_falls_back_like_legacy(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_PROJECTS_MODE", "banana")
        assert ap.resolve("PACOMIND_PROJECTS_MODE",
                          ("off", "shadow", "live"), "shadow") == "shadow"

    def test_sandbox_never_live_from_preset(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        assert ap.resolve("PACOMIND_SANDBOX_MODE",
                          ("off", "dry_run", "live"), "off") == "dry_run"
        # Explicit env is the only path to live.
        monkeypatch.setenv("PACOMIND_SANDBOX_MODE", "live")
        assert ap.resolve("PACOMIND_SANDBOX_MODE",
                          ("off", "dry_run", "live"), "off") == "live"

    def test_passive_turns_everything_off(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "passive")
        snap = ap.snapshot()
        for k, v in snap.items():
            # H4.1: the coupling flag reports its own on/off state, and the
            # loop mode's passive value is "reactive" (the off-equivalent).
            if k in ("preset", "PACOMIND_PRESET_LOOP_COUPLING"):
                continue
            if k == "PACOMIND_AUTONOMY_MODE":
                assert v == "reactive", f"{k}={v} not passive"
                continue
            assert v in ("off", "false"), f"{k}={v} not passive"


class TestSubsystemReadersHonorPreset:
    """The actual mode functions each subsystem exposes resolve through
    the preset (this is what makes the knob real, not decorative)."""

    def test_projects(self, monkeypatch):
        from pacomind.projects.models import projects_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        assert projects_mode() == "live"
        monkeypatch.setenv("PACOMIND_PROJECTS_MODE", "shadow")
        assert projects_mode() == "shadow"

    def test_beliefs(self, monkeypatch):
        from pacomind.beliefs.models import beliefs_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "passive")
        assert beliefs_mode() == "off"

    def test_workers(self, monkeypatch):
        from pacomind.task_queue.governor import workers_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        assert workers_mode() == "live"

    def test_connectors(self, monkeypatch):
        from pacomind.connectors.manager import connectors_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "calibration")
        assert connectors_mode() == "shadow"

    def test_directed(self, monkeypatch):
        from pacomind.directed.service import directed_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "calibration")
        assert directed_mode() == "dry_run"

    def test_sandbox(self, monkeypatch):
        from pacomind.sandbox.manager import sandbox_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        assert sandbox_mode() == "dry_run"

    def test_mining(self, monkeypatch):
        from pacomind.mining.models import mining_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "passive")
        assert mining_mode() == "off"

    def test_skills_distill(self, monkeypatch):
        from pacomind.skills_memory.store import skills_distill_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        assert skills_distill_mode() == "live"

    def test_world_populate(self, monkeypatch):
        from pacomind.world_model.populator import populate_mode
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "passive")
        assert populate_mode() == "off"

    def test_introspection_and_cognition(self, monkeypatch):
        from pacomind.cognition.introspection import introspect_enabled
        from pacomind.cognition.trigger import _cognition_enabled
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "calibration")
        assert introspect_enabled() is True
        assert _cognition_enabled() is True


class TestExpectationsWorkspacePreset:
    """U24 activation: expectations + workspace join the preset table.

    Regression-locks: (a) explicit env still overrides the preset in BOTH
    directions, and (b) deployments that set these env vars explicitly see
    NO behavior change from this activation.
    """

    def _readers(self):
        from pacomind.self_model.expectations import (
            expectations_enabled, expectations_mode,
        )
        from pacomind.self_model.workspace import (
            workspace_enabled, workspace_mode,
        )
        return (expectations_enabled, expectations_mode,
                workspace_enabled, workspace_mode)

    def test_unset_without_preset_stays_off(self):
        exp_on, exp_mode, ws_on, ws_mode = self._readers()
        assert exp_on() is False and exp_mode() == "off"
        assert ws_on() is False and ws_mode() == "off"

    def test_calibration_preset_activates(self, monkeypatch):
        exp_on, exp_mode, ws_on, ws_mode = self._readers()
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "calibration")
        assert exp_on() is True and exp_mode() == "on"
        assert ws_on() is True and ws_mode() == "shadow"

    def test_autonomous_preset_goes_live(self, monkeypatch):
        exp_on, exp_mode, ws_on, ws_mode = self._readers()
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        assert exp_on() is True and exp_mode() == "on"
        assert ws_mode() == "live"

    def test_passive_preset_stays_off(self, monkeypatch):
        exp_on, _, ws_on, ws_mode = self._readers()
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "passive")
        assert exp_on() is False
        assert ws_on() is False and ws_mode() == "off"

    def test_explicit_env_overrides_preset_downward(self, monkeypatch):
        exp_on, exp_mode, _, ws_mode = self._readers()
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("PACOMIND_EXPECTATIONS", "off")
        monkeypatch.setenv("PACOMIND_WORKSPACE", "off")
        assert exp_on() is False and exp_mode() == "off"
        assert ws_mode() == "off"

    def test_explicit_env_overrides_preset_upward(self, monkeypatch):
        exp_on, exp_mode, _, ws_mode = self._readers()
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "passive")
        monkeypatch.setenv("PACOMIND_EXPECTATIONS", "live")
        monkeypatch.setenv("PACOMIND_WORKSPACE", "live")
        assert exp_on() is True and exp_mode() == "live"
        assert ws_mode() == "live"

    def test_env_set_deployments_see_no_change(self, monkeypatch):
        """Every legacy env value resolves exactly as it did before the
        preset integration — with and without an active preset."""
        exp_on, _, _, ws_mode = self._readers()
        legacy_exp = [("off", False), ("shadow", True), ("live", True),
                      ("banana", False)]
        legacy_ws = [("off", "off"), ("shadow", "shadow"),
                     ("live", "live"), ("banana", "off")]
        for preset in (None, "calibration", "autonomous"):
            if preset is None:
                monkeypatch.delenv("PACOMIND_AUTONOMY_PRESET", raising=False)
            else:
                monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", preset)
            for value, want in legacy_exp:
                monkeypatch.setenv("PACOMIND_EXPECTATIONS", value)
                assert exp_on() is want, (preset, value)
            for value, want in legacy_ws:
                monkeypatch.setenv("PACOMIND_WORKSPACE", value)
                assert ws_mode() == want, (preset, value)

    def test_snapshot_reports_both_flags(self, monkeypatch):
        monkeypatch.setenv("PACOMIND_AUTONOMY_PRESET", "calibration")
        snap = ap.snapshot()
        assert snap["PACOMIND_EXPECTATIONS"] == "on"
        assert snap["PACOMIND_WORKSPACE"] == "shadow"
