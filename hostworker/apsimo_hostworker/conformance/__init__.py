"""Executable store-adapter conformance suite for governed host workers.

Any host must pass this suite with its own :class:`StoreHarness` before
running its store live::

    from colony_hostworker.conformance import assert_store_conformance
    assert_store_conformance(my_harness_factory)

The bundled reference store can be checked from the command line::

    python -m colony_hostworker.conformance
"""

from __future__ import annotations

from .harness import (
    HarnessFactory,
    ManualClock,
    SqliteStoreHarness,
    StoreHarness,
    approval_id,
    build_envelope,
    build_intent,
    delivery_gate_evidence,
    grant_gate_evidence,
    sqlite_harness,
)
from .suite import (
    CASES,
    ConformanceFailure,
    ConformanceResult,
    assert_store_conformance,
    run_store_conformance,
)

__all__ = (
    "CASES",
    "ConformanceFailure",
    "ConformanceResult",
    "HarnessFactory",
    "ManualClock",
    "SqliteStoreHarness",
    "StoreHarness",
    "approval_id",
    "assert_store_conformance",
    "build_envelope",
    "build_intent",
    "delivery_gate_evidence",
    "grant_gate_evidence",
    "run_store_conformance",
    "sqlite_harness",
)
