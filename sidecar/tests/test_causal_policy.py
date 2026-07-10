"""H2.1: causal relationship vocabulary + query-only action policy.

Locks: the four causal types exist in both the general vocabulary and the
causal subset; causal_edges_actionable() is OFF unless COLONY_CAUSAL_ACT=1
(query-only invariant holds by default); is_causal() classifies exactly the
causal subset.
"""

from colony_sidecar.world_model.causal_policy import (
    causal_edges_actionable, is_causal,
)
from colony_sidecar.world_model.constants import (
    CAUSAL_RELATIONSHIP_TYPES, RELATIONSHIP_TYPES,
)

_CAUSAL = {"WM_CAUSES", "WM_ENABLES", "WM_BLOCKS", "WM_INHIBITS"}


def test_causal_vocabulary_registered():
    assert CAUSAL_RELATIONSHIP_TYPES == frozenset(_CAUSAL)
    # every causal type is a valid relationship type
    assert CAUSAL_RELATIONSHIP_TYPES <= RELATIONSHIP_TYPES


def test_actionable_defaults_off(monkeypatch):
    """Regression lock: unset/0/junk all keep causal edges query-only."""
    monkeypatch.delenv("COLONY_CAUSAL_ACT", raising=False)
    assert causal_edges_actionable() is False
    for v in ("0", "false", "no", "off", "banana", ""):
        monkeypatch.setenv("COLONY_CAUSAL_ACT", v)
        assert causal_edges_actionable() is False, v


def test_actionable_explicit_unlock(monkeypatch):
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("COLONY_CAUSAL_ACT", v)
        assert causal_edges_actionable() is True, v


def test_is_causal_classifies_subset():
    for t in _CAUSAL:
        assert is_causal(t)
        assert is_causal(t.lower())        # tolerant of case
    assert not is_causal("WM_WORKS_AT")
    assert not is_causal("WM_DEPENDS_ON")  # dependency is not causality
    assert not is_causal("")
    assert not is_causal(None)
