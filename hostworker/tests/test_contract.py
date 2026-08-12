"""Pin the wire contract: golden digests, grammars, and independence."""

import sys

import pytest

from colony_hostworker import contract
from colony_hostworker.contract import (
    GovernedContractError,
    canonical_json_ascii,
    canonical_json_utf8,
    sha256_json_ascii,
    sha256_json_utf8,
)


def test_golden_digest_vectors_both_conventions(golden_vectors):
    saw_divergence = False
    for vector in golden_vectors["digests"]:
        value = vector["value"]
        assert canonical_json_ascii(value) == vector["canonical_ascii"]
        assert canonical_json_utf8(value) == vector["canonical_utf8"]
        assert sha256_json_ascii(value) == vector["sha256_ascii"]
        assert sha256_json_utf8(value) == vector["sha256_utf8"]
        if vector["sha256_ascii"] != vector["sha256_utf8"]:
            saw_divergence = True
    # The two conventions MUST diverge on non-ASCII input; if they ever
    # collapse into one, someone has "unified" the digest split and silently
    # broken one side of the wire.
    assert saw_divergence


def test_conventions_agree_only_on_ascii():
    ascii_value = {"plain": ["ascii", 1, True]}
    assert sha256_json_ascii(ascii_value) == sha256_json_utf8(ascii_value)
    non_ascii = {"text": "café"}
    assert sha256_json_ascii(non_ascii) != sha256_json_utf8(non_ascii)


def test_non_serializable_value_is_refused():
    with pytest.raises(GovernedContractError):
        canonical_json_ascii({"bad": object()})
    with pytest.raises(GovernedContractError):
        canonical_json_utf8({"bad": object()})
    with pytest.raises(GovernedContractError):
        sha256_json_ascii(float("nan"))
    with pytest.raises(GovernedContractError):
        sha256_json_utf8(float("inf"))


def test_schema_names_are_pinned():
    assert contract.INTENT_SCHEMA == "HermesToolActionIntentV1"
    assert contract.INTENT_ENVELOPE_SCHEMA == "HermesToolActionEnvelopeV1"
    assert contract.CALL_IDENTITY_SCHEMA == "HermesActionCallV1"
    assert contract.EXECUTION_REQUEST_SCHEMA == "ColonyGovernedActionExecutionV1"
    assert (
        contract.APPROVAL_BINDING_SCHEMA
        == "ColonyOwnerApprovalExecutionBindingV1"
    )
    assert (
        contract.EXECUTION_RESULT_SCHEMA
        == "ColonyGovernedActionExecutionResultV1"
    )
    assert contract.EFFECT_SCHEMA == "ColonyGovernedActionEffectV1"


def test_identifier_grammars_are_pinned():
    assert contract.APPROVAL_ID_RE.pattern == r"^APR-[A-Z0-9]{12}$"
    assert contract.APPROVAL_ID_RE.fullmatch("APR-7K2M9QX4TR1B")
    assert not contract.APPROVAL_ID_RE.fullmatch("APR-7k2m9qx4tr1b")
    assert not contract.APPROVAL_ID_RE.fullmatch("APR-7K2M9QX4TR1")
    assert contract.INTENT_ID_RE.fullmatch("hti_" + "0" * 32)
    assert not contract.INTENT_ID_RE.fullmatch("hti_" + "0" * 31)
    assert contract.SHA256_RE.fullmatch("a" * 64)
    assert not contract.SHA256_RE.fullmatch("A" * 64)
    assert contract.ACTION_ID_RE.fullmatch(
        "6f1d0a9e-4b2c-4f6e-9a3d-2b1c0d9e8f7a"
    )
    assert not contract.ACTION_ID_RE.fullmatch(
        "6f1d0a9e-4b2c-1f6e-9a3d-2b1c0d9e8f7a"
    )


def test_size_and_time_bounds_are_pinned():
    assert contract.EXECUTION_REQUEST_MAX_BYTES == 32 * 1024
    assert contract.EXECUTION_RESULT_MAX_BYTES == 16 * 1024
    assert contract.EFFECT_MAX_BYTES == 8 * 1024
    assert contract.RESEARCH_TOPIC_MAX_CHARS == 1400
    assert contract.APPROVAL_MAX_LIFETIME_SECONDS == 86_400
    assert contract.GATE_CLOCK_SKEW_SECONDS == 30.0


def test_field_sets_are_pinned():
    assert contract.EXECUTION_REQUEST_FIELDS == frozenset(
        {
            "schema", "version", "action_id", "action_digest", "intent_id",
            "intent_digest", "tool_name", "args", "args_sha256", "approval",
            "execution_digest",
        }
    )
    assert contract.APPROVAL_BINDING_FIELDS == frozenset(
        {
            "schema", "version", "approval_id", "decision_id", "revision",
            "authorization_receipt_sha256", "decided_at", "expires_at",
        }
    )
    assert contract.EFFECT_FIELDS == frozenset(
        {"schema", "version", "effect_id", "outcome", "verification"}
    )
    assert contract.INTENT_FIELDS == frozenset(
        {
            "schema", "version", "intent_id", "idempotency_key", "tool_name",
            "args", "args_sha256", "context", "context_sha256",
            "intent_digest",
        }
    )
    assert contract.CONTEXT_FIELDS == frozenset(
        {
            "api_request_id", "authority_lane", "contact_id", "platform",
            "sender_id", "session_id", "task_id", "tool_call_id", "turn_id",
        }
    )


def test_package_is_stdlib_only_and_server_free():
    """The distribution must never import FastAPI or colony_sidecar.

    This is one half of the deliberate-redundancy rule documented in
    contract.py; the other half (the endpoint never importing this package)
    is enforced by sidecar/tests/test_hostworker_agreement.py.
    """

    import colony_hostworker  # noqa: F401
    import colony_hostworker.catalog  # noqa: F401
    import colony_hostworker.gate  # noqa: F401
    import colony_hostworker.intent  # noqa: F401

    for forbidden in ("fastapi", "colony_sidecar", "httpx", "pydantic"):
        assert forbidden not in sys.modules, (
            "colony_hostworker must stay stdlib-only but imported %s"
            % forbidden
        )


def test_independence_rule_is_written_down():
    docstring = contract.__doc__ or ""
    assert "MUST KEEP" in docstring and "INDEPENDENT" in docstring.upper()
    assert "governed_actions.py" in docstring
