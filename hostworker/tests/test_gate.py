"""Approval-gate invariant core: golden replay + shape-registry law."""

import copy

import pytest

from colony_hostworker.catalog import TOOL_CATALOG
from colony_hostworker.gate import (
    BOUNDED_GRANT_SHAPE,
    GATE_COMMON_FIELDS,
    GRANT_BINDING_METHOD,
    GRANT_UNLIMITED_SENTINEL,
    MESSAGE_DELIVERY_SHAPE,
    OwnerGateError,
    ProvenanceShape,
    ProvenanceShapeError,
    ProvenanceShapeRegistry,
    assert_dispatchable,
    default_registry,
    validate_owner_gate,
)


def replay(case):
    return validate_owner_gate(
        case["action"],
        case["receipts"],
        tool_name=case["tool_name"],
        now=case["now"],
    )


def case_by_name(golden_vectors, name):
    for case in golden_vectors["gate_cases"]:
        if case["name"] == name:
            return copy.deepcopy(case)
    raise AssertionError("missing golden gate case %s" % name)


# ---------------------------------------------------------------- golden replay


def test_golden_gate_cases_replay_identically(golden_vectors):
    """Every recorded worker decision — acceptance, projection, and refusal —
    must replay identically through the extracted invariant core."""

    for case in golden_vectors["gate_cases"]:
        if case["outcome"] == "accepted":
            authorization = replay(case)
            expected = case["expected"]
            assert {
                "receipt_key": authorization.receipt_key,
                "evidence_sha256": authorization.evidence_sha256,
                "approval_id": authorization.approval_id,
                "decision_id": authorization.decision_id,
                "revision": authorization.revision,
                "decided_at": authorization.decided_at,
                "expires_at": authorization.expires_at,
                "expired": authorization.expired,
            } == expected, case["name"]
        else:
            with pytest.raises(OwnerGateError):
                replay(case)


def test_valid_message_delivery_proof_accepted(golden_vectors):
    authorization = replay(case_by_name(golden_vectors, "delivery_valid"))
    assert authorization.shape == "message_delivery"
    assert authorization.granted is False
    assert authorization.expired is False
    assert authorization.revision == 1
    assert_dispatchable(authorization)


def test_valid_grant_proof_accepted(golden_vectors):
    authorization = replay(case_by_name(golden_vectors, "grant_valid"))
    assert authorization.shape == "bounded_grant"
    assert authorization.granted is True
    assert authorization.expired is False
    assert_dispatchable(authorization)


def test_valid_standing_grant_proof_keeps_action_gate_bounded(golden_vectors):
    case = case_by_name(golden_vectors, "grant_valid")
    evidence = case["receipts"][0]["evidence"]
    evidence["bounded_grant_expires_at_epoch"] = GRANT_UNLIMITED_SENTINEL
    from colony_hostworker.contract import sha256_json_utf8

    case["receipts"][0]["evidence_sha256"] = sha256_json_utf8(evidence)

    authorization = replay(case)

    assert authorization.shape == "bounded_grant"
    assert authorization.granted is True
    assert authorization.expired is False
    assert authorization.expires_at == evidence["expires_at_epoch"]
    assert_dispatchable(authorization)


def test_grant_for_non_grantable_tool_refused(golden_vectors):
    for name in (
        "grant_for_non_grantable_tool",
        "grant_for_non_grantable_disable",
        "grant_for_unknown_tool",
    ):
        with pytest.raises(OwnerGateError):
            replay(case_by_name(golden_vectors, name))


def test_expired_grant_refused_at_point_of_use(golden_vectors):
    authorization = replay(
        case_by_name(golden_vectors, "grant_expired_at_point_of_use")
    )
    assert authorization.expired is True
    with pytest.raises(OwnerGateError):
        assert_dispatchable(authorization)


def test_expired_gate_refused_at_point_of_use(golden_vectors):
    authorization = replay(
        case_by_name(golden_vectors, "gate_expired_at_point_of_use")
    )
    assert authorization.expired is True
    with pytest.raises(OwnerGateError):
        assert_dispatchable(authorization)


def test_mixed_and_partial_field_sets_refused(golden_vectors):
    for name in (
        "mixed_shape_delivery_plus_grant_field",
        "partial_shape_missing_event_key",
    ):
        with pytest.raises(OwnerGateError):
            replay(case_by_name(golden_vectors, name))


def test_reserved_binding_method_never_crosses_shapes(golden_vectors):
    for name in (
        "delivery_claiming_grant_binding",
        "grant_claiming_delivery_binding",
    ):
        with pytest.raises(OwnerGateError):
            replay(case_by_name(golden_vectors, name))


def test_evidence_digest_uses_utf8_convention(golden_vectors):
    # A receipt whose evidence_sha256 was computed with the ASCII convention
    # over non-ASCII evidence must be refused: evidence digests are pinned to
    # canonical UTF-8 JSON.
    with pytest.raises(OwnerGateError):
        replay(case_by_name(golden_vectors, "evidence_sha_ascii_convention"))
    # ...while the same evidence with the correct UTF-8 digest is accepted.
    accepted = replay(
        case_by_name(golden_vectors, "delivery_nonascii_principal")
    )
    assert accepted.expired is False


def test_two_gates_refused(golden_vectors):
    case = case_by_name(golden_vectors, "delivery_valid")
    duplicated = copy.deepcopy(case["receipts"][0])
    duplicated["receipt_key"] = "gate-2"
    with pytest.raises(OwnerGateError):
        validate_owner_gate(
            case["action"],
            [case["receipts"][0], duplicated],
            tool_name=case["tool_name"],
            now=case["now"],
        )


def test_zero_gates_refused(golden_vectors):
    case = case_by_name(golden_vectors, "delivery_valid")
    with pytest.raises(OwnerGateError):
        validate_owner_gate(
            case["action"], [], tool_name=case["tool_name"], now=case["now"],
        )


def test_invalid_clock_refused(golden_vectors):
    case = case_by_name(golden_vectors, "delivery_valid")
    for bad_now in (0.0, -1.0, float("nan"), float("inf"), True, "now"):
        with pytest.raises(OwnerGateError):
            validate_owner_gate(
                case["action"],
                case["receipts"],
                tool_name=case["tool_name"],
                now=bad_now,
            )


def test_non_grantable_fails_closed_regardless_of_configuration(
    golden_vectors,
):
    """No registry or catalog configuration can open the grant path for a
    non-grantable tool: an alternate registry still selects a grant shape by
    exact fields, and the catalog's own marker then refuses the tool."""

    case = case_by_name(golden_vectors, "grant_for_non_grantable_tool")
    # Even with a freshly built registry (a hypothetical host mistake that
    # re-registers the built-in shapes), the refusal stands.
    with pytest.raises(OwnerGateError):
        validate_owner_gate(
            case["action"],
            case["receipts"],
            tool_name="colony_autonomy_enable",
            now=case["now"],
            registry=default_registry(),
        )
    # A catalog that simply omits the tool also refuses (fails closed).
    reduced = {
        name: spec
        for name, spec in TOOL_CATALOG.items()
        if name != "colony_autonomy_enable"
    }
    with pytest.raises(OwnerGateError):
        validate_owner_gate(
            case["action"],
            case["receipts"],
            tool_name="colony_autonomy_enable",
            now=case["now"],
            catalog=reduced,
        )


# ---------------------------------------------------------------- registry law


def test_builtin_shapes_are_pinned():
    assert MESSAGE_DELIVERY_SHAPE.evidence_fields == GATE_COMMON_FIELDS | {
        "channel",
        "thread_id",
        "event_id",
        "event_key",
        "delivery_id",
        "delivery_message_id",
        "binding_method",
    }
    assert BOUNDED_GRANT_SHAPE.evidence_fields == GATE_COMMON_FIELDS | {
        "bounded_grant_id",
        "approval_source_request_id",
        "bounded_grant_expires_at_epoch",
        "binding_method",
    }
    assert BOUNDED_GRANT_SHAPE.grants is True
    assert BOUNDED_GRANT_SHAPE.binding_method == GRANT_BINDING_METHOD
    assert BOUNDED_GRANT_SHAPE.expiry_fields == (
        "bounded_grant_expires_at_epoch",
    )
    assert MESSAGE_DELIVERY_SHAPE.grants is False
    assert MESSAGE_DELIVERY_SHAPE.binding_method is None


def test_selection_is_exact_never_subset_or_superset():
    registry = default_registry()
    exact = MESSAGE_DELIVERY_SHAPE.evidence_fields
    assert registry.select(exact) is registry.shapes[0]
    with pytest.raises(OwnerGateError):
        registry.select(exact - {"channel"})
    with pytest.raises(OwnerGateError):
        registry.select(exact | {"extra_field"})
    with pytest.raises(OwnerGateError):
        registry.select(
            exact | BOUNDED_GRANT_SHAPE.evidence_fields
        )


def test_registry_refuses_duplicate_and_overlapping_registrations():
    registry = default_registry()
    with pytest.raises(ProvenanceShapeError):
        registry.register(MESSAGE_DELIVERY_SHAPE)
    # Same field set under a new name: ambiguous.
    clone = ProvenanceShape(
        name="message_delivery_clone",
        provenance_fields=MESSAGE_DELIVERY_SHAPE.provenance_fields,
    )
    with pytest.raises(ProvenanceShapeError):
        registry.register(clone)
    # Superset of a registered shape: overlapping.
    superset = ProvenanceShape(
        name="message_delivery_plus",
        provenance_fields=MESSAGE_DELIVERY_SHAPE.provenance_fields
        | {"extra_field"},
    )
    with pytest.raises(ProvenanceShapeError):
        registry.register(superset)
    # Subset of a registered shape: overlapping.
    subset = ProvenanceShape(
        name="message_delivery_minus",
        provenance_fields=frozenset({"channel", "binding_method"}),
    )
    with pytest.raises(ProvenanceShapeError):
        registry.register(subset)
    # A disjoint provenance set is accepted.
    disjoint = ProvenanceShape(
        name="hardware_token",
        provenance_fields=frozenset({"token_serial", "binding_method"}),
    )
    registry.register(disjoint)
    assert registry.select(disjoint.evidence_fields) is disjoint


def test_bounded_grant_binding_method_is_reserved():
    with pytest.raises(ProvenanceShapeError):
        ProvenanceShape(
            name="fake_grant",
            provenance_fields=frozenset({"other_id", "binding_method"}),
            grants=False,
            binding_method=GRANT_BINDING_METHOD,
        )
    with pytest.raises(ProvenanceShapeError):
        ProvenanceShape(
            name="grant_without_reserved_method",
            provenance_fields=frozenset({"other_id", "binding_method"}),
            grants=True,
            binding_method="something_else",
        )
    # A second shape pinning an already-reserved value is refused.
    registry = default_registry()
    with pytest.raises(ProvenanceShapeError):
        registry.register(
            ProvenanceShape(
                name="second_grant",
                provenance_fields=frozenset(
                    {"grant_ref", "grant_ref_expires_at_epoch",
                     "binding_method"}
                ),
                grants=True,
                binding_method=GRANT_BINDING_METHOD,
                expiry_fields=("grant_ref_expires_at_epoch",),
            )
        )


def test_open_shapes_refuse_values_reserved_by_other_shapes():
    registry = default_registry()
    assert registry.binding_method_valid(
        MESSAGE_DELIVERY_SHAPE, "single_use_code"
    )
    assert registry.binding_method_valid(MESSAGE_DELIVERY_SHAPE, "bound_reply")
    assert not registry.binding_method_valid(
        MESSAGE_DELIVERY_SHAPE, GRANT_BINDING_METHOD
    )
    assert not registry.binding_method_valid(MESSAGE_DELIVERY_SHAPE, "")
    assert not registry.binding_method_valid(MESSAGE_DELIVERY_SHAPE, None)
    assert registry.binding_method_valid(
        BOUNDED_GRANT_SHAPE, GRANT_BINDING_METHOD
    )
    assert not registry.binding_method_valid(
        BOUNDED_GRANT_SHAPE, "single_use_code"
    )


def test_shape_construction_law():
    with pytest.raises(ProvenanceShapeError):
        ProvenanceShape(
            name="no_binding_method",
            provenance_fields=frozenset({"proof_id"}),
        )
    with pytest.raises(ProvenanceShapeError):
        ProvenanceShape(
            name="shadows_common_field",
            provenance_fields=frozenset({"decision", "binding_method"}),
        )
    with pytest.raises(ProvenanceShapeError):
        ProvenanceShape(
            name="expiry_not_in_provenance",
            provenance_fields=frozenset({"proof_id", "binding_method"}),
            expiry_fields=("missing_epoch",),
        )
    with pytest.raises(ProvenanceShapeError):
        ProvenanceShape(
            name="binding_method_as_expiry",
            provenance_fields=frozenset({"proof_id", "binding_method"}),
            expiry_fields=("binding_method",),
        )


def test_custom_shape_expiries_are_reenforced(golden_vectors):
    """Every expiry a shape declares is re-enforced at point of use."""

    registry = ProvenanceShapeRegistry((MESSAGE_DELIVERY_SHAPE,))
    two_expiry_shape = ProvenanceShape(
        name="double_expiry_grant",
        provenance_fields=frozenset(
            {
                "grant_ref",
                "first_expires_at_epoch",
                "second_expires_at_epoch",
                "binding_method",
            }
        ),
        grants=True,
        binding_method=GRANT_BINDING_METHOD,
        expiry_fields=("first_expires_at_epoch", "second_expires_at_epoch"),
    )
    registry.register(two_expiry_shape)
    base = case_by_name(golden_vectors, "grant_valid")
    evidence = dict(base["receipts"][0]["evidence"])
    for name in (
        "bounded_grant_id",
        "approval_source_request_id",
        "bounded_grant_expires_at_epoch",
    ):
        del evidence[name]
    now = base["now"]
    evidence.update(
        grant_ref="grant-ref-1",
        first_expires_at_epoch=now + 100.0,
        second_expires_at_epoch=now + 100.0,
    )

    from colony_hostworker.contract import sha256_json_utf8

    def receipt_for(evidence_document):
        return {
            "kind": "gate",
            "status": "passed",
            "external_id": evidence_document["approval_id"],
            "evidence": evidence_document,
            "evidence_sha256": sha256_json_utf8(evidence_document),
            "receipt_key": "gate-1",
        }

    live = validate_owner_gate(
        base["action"],
        [receipt_for(evidence)],
        tool_name="colony_record_insight",
        now=now,
        registry=registry,
    )
    assert live.expired is False
    # Either expiry lapsing flips the authorization to expired.
    for lapsed_field in ("first_expires_at_epoch", "second_expires_at_epoch"):
        lapsed = dict(evidence)
        lapsed[lapsed_field] = now - 1.0
        expired = validate_owner_gate(
            base["action"],
            [receipt_for(lapsed)],
            tool_name="colony_record_insight",
            now=now,
            registry=registry,
        )
        assert expired.expired is True
        with pytest.raises(OwnerGateError):
            assert_dispatchable(expired)
