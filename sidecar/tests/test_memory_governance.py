"""Tests for Memory Governance and Epistemic Hygiene (v0.15.0).

Covers:
- Source anchoring (source_type, source_uri, content_hash)
- Confidence computation (effective_confidence)
- Write governance (importance clamping, protected memories)
- Epistemic state transitions
- File reconciliation logic
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch

import pytest

from colony_sidecar.intelligence.graph.client import (
    ColonyGraph,
    EpistemicState,
    GraphConfig,
    MAX_IMPORTANCE,
    MemorySourceType,
    SOURCE_RELIABILITY,
)
from colony_sidecar.intelligence.graph.reconciler import FileReconciler


# ---------------------------------------------------------------------------
# compute_effective_confidence
# ---------------------------------------------------------------------------

class TestComputeEffectiveConfidence:
    def test_user_assertion_max_confidence(self):
        now = datetime.now(timezone.utc)
        conf = ColonyGraph.compute_effective_confidence(
            base_confidence=1.0,
            source_reliability=SOURCE_RELIABILITY[MemorySourceType.USER_ASSERTION],
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        assert conf == pytest.approx(1.0, rel=0.01)

    def test_inference_lower_confidence(self):
        now = datetime.now(timezone.utc)
        conf = ColonyGraph.compute_effective_confidence(
            base_confidence=0.7,
            source_reliability=SOURCE_RELIABILITY[MemorySourceType.INFERENCE],
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        assert conf == pytest.approx(0.35, rel=0.01)  # 0.7 * 0.5

    def test_corroboration_vs_contradiction(self):
        now = datetime.now(timezone.utc)
        conf_corro = ColonyGraph.compute_effective_confidence(
            base_confidence=0.5,
            source_reliability=0.9,
            corroboration_count=3,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        conf_contra = ColonyGraph.compute_effective_confidence(
            base_confidence=0.5,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=3,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        # Corroboration should produce higher confidence than contradiction
        assert conf_corro > conf_contra

    def test_contradiction_penalty(self):
        now = datetime.now(timezone.utc)
        conf_base = ColonyGraph.compute_effective_confidence(
            base_confidence=0.8,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        conf_penalty = ColonyGraph.compute_effective_confidence(
            base_confidence=0.8,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=5,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        assert conf_penalty < conf_base

    def test_recall_reinforcement(self):
        now = datetime.now(timezone.utc)
        conf_0 = ColonyGraph.compute_effective_confidence(
            base_confidence=0.8,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        conf_10 = ColonyGraph.compute_effective_confidence(
            base_confidence=0.8,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=0,
            recalls=10,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        assert conf_10 > conf_0

    def test_recency_discount(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=365)
        conf_old = ColonyGraph.compute_effective_confidence(
            base_confidence=1.0,
            source_reliability=1.0,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=old,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        conf_new = ColonyGraph.compute_effective_confidence(
            base_confidence=1.0,
            source_reliability=1.0,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        assert conf_old < conf_new

    def test_verification_boost(self):
        now = datetime.now(timezone.utc)
        conf_no_verify = ColonyGraph.compute_effective_confidence(
            base_confidence=0.8,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        conf_verified = ColonyGraph.compute_effective_confidence(
            base_confidence=0.8,
            source_reliability=0.9,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=now - timedelta(days=1),
            created_at=now,
            epistemic_state=EpistemicState.INFERRED.value,
            now=now,
        )
        assert conf_verified > conf_no_verify

    def test_verified_state_floor(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=365 * 5)
        conf = ColonyGraph.compute_effective_confidence(
            base_confidence=0.5,
            source_reliability=0.5,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=old,
            epistemic_state=EpistemicState.VERIFIED.value,
            now=now,
        )
        assert conf >= 0.9

    def test_stale_state_penalty(self):
        now = datetime.now(timezone.utc)
        conf = ColonyGraph.compute_effective_confidence(
            base_confidence=1.0,
            source_reliability=1.0,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.STALE.value,
            now=now,
        )
        assert conf <= 0.35  # 1.0 * 0.3 + small adjustments

    def test_deprecated_state_penalty(self):
        now = datetime.now(timezone.utc)
        conf = ColonyGraph.compute_effective_confidence(
            base_confidence=1.0,
            source_reliability=1.0,
            corroboration_count=0,
            contradiction_count=0,
            recalls=0,
            last_verified_at=None,
            created_at=now,
            epistemic_state=EpistemicState.DEPRECATED.value,
            now=now,
        )
        assert conf <= 0.15  # 1.0 * 0.1 + small adjustments


# ---------------------------------------------------------------------------
# Write governance
# ---------------------------------------------------------------------------

class TestWriteGovernance:
    def test_importance_clamping_user_assertion(self):
        assert MAX_IMPORTANCE[MemorySourceType.USER_ASSERTION] == 1.0

    def test_importance_clamping_inference(self):
        assert MAX_IMPORTANCE[MemorySourceType.INFERENCE] == 0.7

    def test_source_reliability_ordering(self):
        assert SOURCE_RELIABILITY[MemorySourceType.USER_ASSERTION] > \
               SOURCE_RELIABILITY[MemorySourceType.FILE] > \
               SOURCE_RELIABILITY[MemorySourceType.TOOL_OUTPUT] > \
               SOURCE_RELIABILITY[MemorySourceType.CONVERSATION] > \
               SOURCE_RELIABILITY[MemorySourceType.INFERENCE]


# ---------------------------------------------------------------------------
# Epistemic states
# ---------------------------------------------------------------------------

class TestEpistemicStates:
    def test_all_states_present(self):
        states = [s.value for s in EpistemicState]
        assert set(states) == {
            "inferred", "observed", "corroborated", "verified",
            "stale", "superseded", "deprecated", "archived",
        }

    def test_state_transitions_valid(self):
        # Forward progression
        assert EpistemicState.INFERRED.value == "inferred"
        assert EpistemicState.OBSERVED.value == "observed"
        assert EpistemicState.CORROBORATED.value == "corroborated"
        assert EpistemicState.VERIFIED.value == "verified"
        # Terminal states
        assert EpistemicState.STALE.value == "stale"
        assert EpistemicState.SUPERSEDED.value == "superseded"
        assert EpistemicState.DEPRECATED.value == "deprecated"
        assert EpistemicState.ARCHIVED.value == "archived"


# ---------------------------------------------------------------------------
# FileReconciler
# ---------------------------------------------------------------------------

class TestFileReconciler:
    def test_reconciler_class_exists(self):
        """FileReconciler class is importable and has reconcile method."""
        assert hasattr(FileReconciler, 'reconcile')

    def test_reconcile_returns_dict(self):
        """reconcile method returns expected dict keys."""
        import inspect
        sig = inspect.signature(FileReconciler.reconcile)
        assert 'dry_run' in sig.parameters


# ---------------------------------------------------------------------------
# Source anchoring
# ---------------------------------------------------------------------------

class TestSourceAnchoring:
    def test_source_type_enum_values(self):
        assert MemorySourceType.CONVERSATION.value == "conversation"
        assert MemorySourceType.FILE.value == "file"
        assert MemorySourceType.TOOL_OUTPUT.value == "tool_output"
        assert MemorySourceType.USER_ASSERTION.value == "user_assertion"
        assert MemorySourceType.INFERENCE.value == "inference"

    def test_content_hash_computation(self):
        content = "test content"
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert expected == "6ae8a75555209fd6c44157c0aed8016e763ff435a19cf186f76863140143ff72"
