"""Observability-honesty: the system must report its true health.

1. _phase_cognition must surface MetaLearner cycle errors (it previously
   discarded run_cycle()'s result, so a fully-degraded cognition cycle looked
   like a clean tick).
2. The memory_consolidate scheduler stat must read the real field
   (pairs_merged), not a non-existent merged_count that always reported 0.
"""
import dataclasses
from types import SimpleNamespace

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop


class _StubCognition:
    def __init__(self, errors):
        self._errors = errors

    async def run_cycle(self):
        return SimpleNamespace(errors=self._errors)


@pytest.mark.asyncio
async def test_cognition_step_errors_are_counted_and_warned():
    loop = AutonomyLoop(registry=SimpleNamespace(cognition=_StubCognition(["e1", "e2", "e3"])))
    await loop._phase_cognition()
    assert loop.stats.errors == 3


@pytest.mark.asyncio
async def test_cognition_clean_cycle_counts_no_errors():
    loop = AutonomyLoop(registry=SimpleNamespace(cognition=_StubCognition([])))
    await loop._phase_cognition()
    assert loop.stats.errors == 0


@pytest.mark.asyncio
async def test_cognition_result_without_errors_attr_is_safe():
    class _C:
        async def run_cycle(self):
            return None  # no .errors attr — must not crash or miscount

    loop = AutonomyLoop(registry=SimpleNamespace(cognition=_C()))
    await loop._phase_cognition()
    assert loop.stats.errors == 0


@pytest.mark.asyncio
async def test_cognition_none_registry_is_noop():
    loop = AutonomyLoop(registry=SimpleNamespace(cognition=None))
    await loop._phase_cognition()
    assert loop.stats.errors == 0


def test_consolidation_result_exposes_pairs_merged_not_merged_count():
    from colony_sidecar.intelligence.graph.consolidator import ConsolidationResult
    names = {f.name for f in dataclasses.fields(ConsolidationResult)}
    assert "pairs_merged" in names
    assert "merged_count" not in names
