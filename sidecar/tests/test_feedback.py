"""Tests for outcome-driven per-type priority feedback (item 3b / correction 4)."""

from __future__ import annotations

from colony_sidecar.feedback import TypeFeedbackStore


def test_actioned_boosts_dismissed_decays():
    s = TypeFeedbackStore(db_path=None)
    assert s.multiplier("research") == 1.0
    m1 = s.record("research", "actioned")
    assert m1 > 1.0
    m2 = s.record("relationship", "dismissed")
    assert m2 < 1.0
    assert s.multiplier("research") > 1.0
    assert s.multiplier("relationship") < 1.0


def test_multiplier_is_clamped():
    s = TypeFeedbackStore(db_path=None)
    for _ in range(50):
        s.record("t", "dismissed")
    assert s.multiplier("t") >= 0.5   # floor
    for _ in range(80):
        s.record("t", "actioned")
    assert s.multiplier("t") <= 1.5   # ceiling


def test_snapshot_counts_outcomes():
    s = TypeFeedbackStore(db_path=None)
    s.record("research", "actioned")
    s.record("research", "dismissed")
    snap = {r["itype"]: r for r in s.snapshot()}
    assert snap["research"]["actioned"] == 1
    assert snap["research"]["dismissed"] == 1
