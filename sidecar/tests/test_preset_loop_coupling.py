"""H4.1 — preset <-> loop-mode coupling (COLONY_PRESET_LOOP_COUPLING).

The one deliberate default-flip of the hardening program: an active
COLONY_AUTONOMY_PRESET now supplies the autonomy loop mode when
COLONY_AUTONOMY_MODE is unset. Resolution precedence:

    explicit env  >  coupled preset  >  legacy tick migration  >  default

Regression locks: explicit env is the rollback path and always wins;
COLONY_PRESET_LOOP_COUPLING=off restores today's env-only behavior exactly;
coupling errors fail toward reactive.
"""

import dataclasses

import pytest

from colony_sidecar.autonomy.config import AutonomyConfig, AutonomyMode
from colony_sidecar.util import autonomy_preset as ap

_ENV = [
    "COLONY_AUTONOMY_PRESET", "COLONY_AUTONOMY_MODE",
    "COLONY_PRESET_LOOP_COUPLING", "COLONY_AUTONOMY_TICK_INTERVAL_SECS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


class TestPresetTable:
    def test_every_preset_carries_a_loop_mode(self):
        assert ap.PRESETS["passive"]["COLONY_AUTONOMY_MODE"] == "reactive"
        assert ap.PRESETS["calibration"]["COLONY_AUTONOMY_MODE"] == "proactive"
        assert ap.PRESETS["autonomous"]["COLONY_AUTONOMY_MODE"] == "proactive"

    def test_snapshot_reports_coupling_and_mode(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        snap = ap.snapshot()
        assert snap["COLONY_PRESET_LOOP_COUPLING"] == "on"
        assert snap["COLONY_AUTONOMY_MODE"] == "proactive"

    def test_snapshot_coupling_off(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        monkeypatch.setenv("COLONY_PRESET_LOOP_COUPLING", "off")
        snap = ap.snapshot()
        assert snap["COLONY_PRESET_LOOP_COUPLING"] == "off"
        assert snap["COLONY_AUTONOMY_MODE"] == "reactive"

    def test_snapshot_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "calibration")
        monkeypatch.setenv("COLONY_AUTONOMY_MODE", "reactive")
        assert ap.snapshot()["COLONY_AUTONOMY_MODE"] == "reactive"


class TestResolutionPrecedence:
    """The full env > coupled-preset > legacy-tick > default matrix."""

    def test_default_is_reactive(self):
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "default"

    def test_coupled_preset_supplies_mode(self, monkeypatch):
        for preset, want in (("passive", AutonomyMode.REACTIVE),
                             ("calibration", AutonomyMode.PROACTIVE),
                             ("autonomous", AutonomyMode.PROACTIVE)):
            monkeypatch.setenv("COLONY_AUTONOMY_PRESET", preset)
            cfg = AutonomyConfig.from_env()
            assert cfg.mode is want, preset
            assert cfg.mode_source == "preset", preset

    def test_explicit_env_beats_coupled_preset(self, monkeypatch):
        """The rollback path: COLONY_AUTONOMY_MODE=reactive under a preset."""
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("COLONY_AUTONOMY_MODE", "reactive")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "env"
        # ...and upward, too (proactive without any preset help).
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        monkeypatch.setenv("COLONY_AUTONOMY_MODE", "proactive")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.PROACTIVE
        assert cfg.mode_source == "env"

    def test_coupled_preset_beats_legacy_tick(self, monkeypatch):
        """passive + legacy tick: the preset's reactive wins over the old
        tick-implies-proactive migration."""
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        monkeypatch.setenv("COLONY_AUTONOMY_TICK_INTERVAL_SECS", "60")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "preset"

    def test_legacy_tick_beats_default(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_TICK_INTERVAL_SECS", "60")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.PROACTIVE
        assert cfg.mode_source == "legacy_tick"

    def test_invalid_explicit_env_still_counts_as_env(self, monkeypatch):
        """A set-but-invalid COLONY_AUTONOMY_MODE falls back to reactive
        exactly as the legacy reader did (preset must NOT resurrect it)."""
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("COLONY_AUTONOMY_MODE", "banana")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "env"


class TestCouplingOffRegressionLock:
    """COLONY_PRESET_LOOP_COUPLING=off = today's behavior, exactly."""

    def test_coupling_off_ignores_preset(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("COLONY_PRESET_LOOP_COUPLING", "off")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "default"

    def test_coupling_off_config_identical_to_no_preset(self, monkeypatch):
        baseline = AutonomyConfig.from_env()
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setenv("COLONY_PRESET_LOOP_COUPLING", "off")
        assert dataclasses.asdict(AutonomyConfig.from_env()) == \
            dataclasses.asdict(baseline)

    def test_coupling_off_legacy_tick_migration_survives(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "passive")
        monkeypatch.setenv("COLONY_PRESET_LOOP_COUPLING", "off")
        monkeypatch.setenv("COLONY_AUTONOMY_TICK_INTERVAL_SECS", "60")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.PROACTIVE
        assert cfg.mode_source == "legacy_tick"


class TestFailSafe:
    def test_coupling_error_fails_toward_reactive(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")

        def _boom():
            raise RuntimeError("coupling machinery broke")
        monkeypatch.setattr(ap, "coupled_loop_mode", _boom)
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "default"

    def test_coupled_loop_mode_never_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError("preset store broke")
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "autonomous")
        monkeypatch.setattr(ap, "preset_name", _boom)
        assert ap.coupled_loop_mode() is None

    def test_unknown_preset_stays_default(self, monkeypatch):
        monkeypatch.setenv("COLONY_AUTONOMY_PRESET", "yolo")
        cfg = AutonomyConfig.from_env()
        assert cfg.mode is AutonomyMode.REACTIVE
        assert cfg.mode_source == "default"
