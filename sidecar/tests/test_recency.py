"""Phase 4: configurable recency weighting for retrieval (v0.21.0)."""

from colony_sidecar.intelligence.graph.client import _recency_factor


def test_recency_defaults(monkeypatch):
    monkeypatch.delenv("COLONY_RECENCY_HALF_LIFE_DAYS", raising=False)
    monkeypatch.delenv("COLONY_RECENCY_FLOOR", raising=False)
    assert _recency_factor(0) == 1.0
    assert abs(_recency_factor(90) - 0.75) < 0.01     # one half-life -> floor + half
    assert 0.49 < _recency_factor(100000) <= 0.51      # approaches the 0.5 floor
    # strictly decreasing with age
    assert _recency_factor(0) > _recency_factor(30) > _recency_factor(365)
    # recent vs old gap is now material (was ~0.1 over a YEAR before)
    assert _recency_factor(0) - _recency_factor(365) > 0.4


def test_recency_configurable(monkeypatch):
    monkeypatch.setenv("COLONY_RECENCY_HALF_LIFE_DAYS", "30")
    monkeypatch.setenv("COLONY_RECENCY_FLOOR", "0.2")
    assert abs(_recency_factor(30) - (0.2 + 0.8 * 0.5)) < 0.01   # 0.6
    assert _recency_factor(0) == 1.0


def test_recency_disabled(monkeypatch):
    monkeypatch.setenv("COLONY_RECENCY_HALF_LIFE_DAYS", "0")
    assert _recency_factor(9999) == 1.0
