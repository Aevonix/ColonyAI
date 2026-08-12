"""Stateless, stdlib-only core for governed Colony host workers.

Phase A of the host-worker extraction: the wire contract, the governed tool
catalog, ``HermesToolActionIntentV1`` validation, and the approval-gate
invariant core.  This distribution must never import FastAPI or
``colony_sidecar`` — see the design rule in :mod:`colony_hostworker.contract`.
"""

from __future__ import annotations

from .catalog import (
    ACTION_TOOL_NAMES,
    GRANT_AUTHORIZABLE_TOOL_NAMES,
    NON_GRANTABLE_TOOL_NAMES,
    TOOL_CATALOG,
    ToolCatalogError,
    ToolSpec,
    validate_tool_args,
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

__version__ = "0.1.0"

__all__ = (
    "ACTION_TOOL_NAMES",
    "BOUNDED_GRANT_SHAPE",
    "DEFAULT_REGISTRY",
    "GRANT_AUTHORIZABLE_TOOL_NAMES",
    "GRANT_BINDING_METHOD",
    "GateAuthorization",
    "GovernedContractError",
    "HermesActionIntentError",
    "HermesToolActionIntentV1",
    "MESSAGE_DELIVERY_SHAPE",
    "NON_GRANTABLE_TOOL_NAMES",
    "OwnerGateError",
    "ProvenanceShape",
    "ProvenanceShapeError",
    "ProvenanceShapeRegistry",
    "TOOL_CATALOG",
    "ToolCatalogError",
    "ToolSpec",
    "assert_dispatchable",
    "canonical_json_ascii",
    "canonical_json_utf8",
    "default_registry",
    "sha256_json_ascii",
    "sha256_json_utf8",
    "validate_owner_gate",
    "validate_tool_args",
)
