"""Tests for the persona deployment framework."""

import json
from pathlib import Path

import pytest

from colony_sidecar.persona.engine import (
    PersonaEngine,
    _has_circular_deps,
    _resolve_templates,
    _topo_sort,
)


# We can't import Pydantic-based manifest on this machine, so we
# test the engine internals that don't depend on it.


# ── Template resolution ──────────────────────────────────────────────────


class TestTemplateResolution:
    def test_simple_replacement(self):
        result = _resolve_templates("host={{ host }}:{{ port }}", {"host": "192.168.1.1", "port": "8080"})
        assert result == "host=192.168.1.1:8080"

    def test_missing_variable_preserved(self):
        result = _resolve_templates("{{ known }}={{ unknown }}", {"known": "value"})
        assert result == "value={{ unknown }}"

    def test_no_templates(self):
        result = _resolve_templates("plain text", {})
        assert result == "plain text"

    def test_spaced_braces(self):
        result = _resolve_templates("{{  host  }}", {"host": "localhost"})
        assert result == "localhost"


# ── Dependency graph ─────────────────────────────────────────────────────


class _FakeSvc:
    def __init__(self, name, depends_on=None):
        self.name = name
        self.depends_on = depends_on or []


class TestDependencyGraph:
    def test_no_circular(self):
        svcs = [_FakeSvc("a"), _FakeSvc("b", ["a"]), _FakeSvc("c", ["b"])]
        assert _has_circular_deps(svcs) is False

    def test_circular_detected(self):
        svcs = [_FakeSvc("a", ["c"]), _FakeSvc("b", ["a"]), _FakeSvc("c", ["b"])]
        assert _has_circular_deps(svcs) is True

    def test_self_loop(self):
        svcs = [_FakeSvc("a", ["a"])]
        assert _has_circular_deps(svcs) is True

    def test_empty(self):
        assert _has_circular_deps([]) is False


class TestTopoSort:
    def test_linear_chain(self):
        svcs = [_FakeSvc("c", ["b"]), _FakeSvc("b", ["a"]), _FakeSvc("a")]
        order = [s.name for s in _topo_sort(svcs)]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_independent_services(self):
        svcs = [_FakeSvc("a"), _FakeSvc("b"), _FakeSvc("c")]
        order = [s.name for s in _topo_sort(svcs)]
        assert set(order) == {"a", "b", "c"}

    def test_diamond_dependency(self):
        svcs = [
            _FakeSvc("d", ["b", "c"]),
            _FakeSvc("b", ["a"]),
            _FakeSvc("c", ["a"]),
            _FakeSvc("a"),
        ]
        order = [s.name for s in _topo_sort(svcs)]
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_external_dep_skipped(self):
        svcs = [_FakeSvc("a", ["hermes"]), _FakeSvc("b", ["a"])]
        order = [s.name for s in _topo_sort(svcs)]
        assert order == ["a", "b"]
