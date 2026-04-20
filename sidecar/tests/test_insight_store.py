"""Tests for InsightStore dismiss/list/undismiss semantics."""

from __future__ import annotations

from colony_sidecar.intelligence.synthesis.insight_store import InsightStore


def test_dismiss_then_list(tmp_path):
    store = InsightStore(tmp_path / "insights.db")
    assert store.list_dismissed() == set()
    store.dismiss("insight-1")
    assert store.is_dismissed("insight-1")
    assert store.list_dismissed() == {"insight-1"}


def test_dismiss_is_idempotent(tmp_path):
    store = InsightStore(tmp_path / "insights.db")
    store.dismiss("insight-1")
    store.dismiss("insight-1")
    assert store.list_dismissed() == {"insight-1"}


def test_undismiss(tmp_path):
    store = InsightStore(tmp_path / "insights.db")
    store.dismiss("i")
    assert store.undismiss("i") is True
    assert store.list_dismissed() == set()
    assert store.undismiss("i") is False  # already gone


def test_store_persists_across_instances(tmp_path):
    db = tmp_path / "insights.db"
    InsightStore(db).dismiss("persisted")
    assert InsightStore(db).is_dismissed("persisted")
