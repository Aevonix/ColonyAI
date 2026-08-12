"""Stdlib-only core for governed Colony host workers.

Phase A of the host-worker extraction: the wire contract, the governed tool
catalog, ``HermesToolActionIntentV1`` validation, and the approval-gate
invariant core.  Phase B: the loopback execution client, the dispatch
admission, the :class:`~colony_hostworker.store.ActionStore` protocol with
its documented transactional invariants, the reference SQLite store, the
one-mutation worker, and the executable store conformance suite
(:mod:`colony_hostworker.conformance`).  This distribution must never
import FastAPI or ``colony_sidecar`` — see the design rule in
:mod:`colony_hostworker.contract`.
"""

from __future__ import annotations

from .admission import (
    DispatchAdmission,
    DispatchAdmissionError,
    FileDispatchAdmission,
    sqlite_database_identity,
)
from .catalog import (
    ACTION_TOOL_NAMES,
    GRANT_AUTHORIZABLE_TOOL_NAMES,
    NON_GRANTABLE_TOOL_NAMES,
    TOOL_CATALOG,
    ToolCatalogError,
    ToolSpec,
    validate_tool_args,
)
from .client import (
    ClientCredential,
    GovernedActionClient,
    GovernedActionClientError,
    WORKER_PRINCIPAL,
    build_no_redirect_opener,
)
from .contract import (
    GovernedContractError,
    canonical_json_ascii,
    canonical_json_utf8,
    sha256_json_ascii,
    sha256_json_utf8,
)
from .gate import (
    BOUNDED_GRANT_SHAPE,
    DEFAULT_REGISTRY,
    GRANT_BINDING_METHOD,
    GateAuthorization,
    MESSAGE_DELIVERY_SHAPE,
    OwnerGateError,
    ProvenanceShape,
    ProvenanceShapeError,
    ProvenanceShapeRegistry,
    assert_dispatchable,
    default_registry,
    validate_owner_gate,
)
from .intent import HermesActionIntentError, HermesToolActionIntentV1
from .sqlite_store import SqliteActionStore
from .store import (
    ActionIdempotencyConflict,
    ActionLeaseConflict,
    ActionNotFound,
    ActionStore,
    ActionStoreError,
    ActionTransitionError,
)
from .worker import (
    GovernedActionWorker,
    GovernedActionWorkerError,
    build_execution_request,
    validate_execution_result,
)

__version__ = "0.2.0"

__all__ = (
    "ACTION_TOOL_NAMES",
    "ActionIdempotencyConflict",
    "ActionLeaseConflict",
    "ActionNotFound",
    "ActionStore",
    "ActionStoreError",
    "ActionTransitionError",
    "BOUNDED_GRANT_SHAPE",
    "ClientCredential",
    "DEFAULT_REGISTRY",
    "DispatchAdmission",
    "DispatchAdmissionError",
    "FileDispatchAdmission",
    "GRANT_AUTHORIZABLE_TOOL_NAMES",
    "GRANT_BINDING_METHOD",
    "GateAuthorization",
    "GovernedActionClient",
    "GovernedActionClientError",
    "GovernedActionWorker",
    "GovernedActionWorkerError",
    "GovernedContractError",
    "HermesActionIntentError",
    "HermesToolActionIntentV1",
    "MESSAGE_DELIVERY_SHAPE",
    "NON_GRANTABLE_TOOL_NAMES",
    "OwnerGateError",
    "ProvenanceShape",
    "ProvenanceShapeError",
    "ProvenanceShapeRegistry",
    "SqliteActionStore",
    "TOOL_CATALOG",
    "ToolCatalogError",
    "ToolSpec",
    "WORKER_PRINCIPAL",
    "assert_dispatchable",
    "build_execution_request",
    "build_no_redirect_opener",
    "canonical_json_ascii",
    "canonical_json_utf8",
    "default_registry",
    "sha256_json_ascii",
    "sha256_json_utf8",
    "sqlite_database_identity",
    "validate_execution_result",
    "validate_owner_gate",
    "validate_tool_args",
)
