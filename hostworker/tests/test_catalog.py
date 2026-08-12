"""The catalog is the single source of truth for governed tools."""

import pytest

from colony_hostworker.catalog import (
    ACTION_TOOL_NAMES,
    GRANT_AUTHORIZABLE_TOOL_NAMES,
    NON_GRANTABLE_TOOL_NAMES,
    TOOL_CATALOG,
    ToolCatalogError,
    validate_tool_args,
)
from colony_hostworker.contract import GovernedContractError, sha256_json_ascii


def test_catalog_contains_exactly_the_governed_tools():
    assert ACTION_TOOL_NAMES == frozenset(
        {
            "colony_autonomy_disable",
            "colony_autonomy_enable",
            "colony_create_commitment",
            "colony_initiative_feedback",
            "colony_record_insight",
            "colony_research",
            "colony_resolve_commitment",
            "colony_task_complete",
            "colony_task_dismiss",
            "colony_task_snooze",
        }
    )


def test_autonomy_tools_are_non_grantable():
    # Owner decision: autonomy posture must always be a per-message owner
    # approval and can never be authorized by a standing grant.
    assert NON_GRANTABLE_TOOL_NAMES == frozenset(
        {"colony_autonomy_enable", "colony_autonomy_disable"}
    )
    # This must stay the exact complement used by the deployed worker's grant
    # allowlist (_GRANT_AUTHORIZABLE_TOOLS).
    assert GRANT_AUTHORIZABLE_TOOL_NAMES == frozenset(
        {
            "colony_create_commitment",
            "colony_initiative_feedback",
            "colony_record_insight",
            "colony_research",
            "colony_resolve_commitment",
            "colony_task_complete",
            "colony_task_dismiss",
            "colony_task_snooze",
        }
    )


def test_golden_valid_args_accepted_and_normalized(golden_vectors):
    for vector in golden_vectors["valid_args"]:
        normalized = validate_tool_args(vector["tool_name"], vector["args"])
        assert normalized == vector["normalized"], vector
        assert sha256_json_ascii(normalized) == vector["args_sha256_ascii"]


def test_golden_invalid_args_refused(golden_vectors):
    for vector in golden_vectors["invalid_args"]:
        with pytest.raises(GovernedContractError):
            validate_tool_args(vector["tool_name"], vector["args"])


def test_every_endpoint_tool_has_a_golden_valid_vector(golden_vectors):
    """Each governed tool is exercised by at least one golden vector."""

    assert set(golden_vectors["action_tool_names"]) == set(ACTION_TOOL_NAMES)
    covered = {vector["tool_name"] for vector in golden_vectors["valid_args"]}
    assert covered == set(ACTION_TOOL_NAMES)


def test_unknown_tool_is_refused():
    with pytest.raises(ToolCatalogError):
        validate_tool_args("colony_send_message", {})
    with pytest.raises(ToolCatalogError):
        validate_tool_args(None, {})


def test_validators_reject_non_mapping_args():
    for name in ACTION_TOOL_NAMES:
        with pytest.raises(GovernedContractError):
            validate_tool_args(name, "not-an-object")


def test_approval_display_matches_deployed_worker_wording():
    display = TOOL_CATALOG["colony_task_snooze"].approval_display(
        {"task_id": "task-1", "hours": 3}
    )
    assert display == {
        "summary": 'Snooze Colony task "task-1" for 3 hours',
        "target": "Private Colony task or initiative ledger",
        "risk": (
            "Defers one internal task or initiative until the bounded "
            "snooze expires"
        ),
    }
    display = TOOL_CATALOG["colony_autonomy_enable"].approval_display({})
    assert display == {
        "summary": "Enable Colony autonomous scheduling",
        "target": "Colony autonomy scheduler",
        "risk": (
            "Colony may begin bounded autonomous work under its configured "
            "policies"
        ),
    }
    # Long values are ellipsized at 300 characters exactly like the deployed
    # worker's _preview (299 characters + one ellipsis, JSON-quoted ASCII).
    display = TOOL_CATALOG["colony_create_commitment"].approval_display(
        {"description": "d" * 400}
    )
    assert display["summary"] == (
        'Create Colony commitment "%s\\u2026"' % ("d" * 299)
    )


def test_every_tool_has_display_metadata():
    samples = {
        "colony_autonomy_disable": {},
        "colony_autonomy_enable": {},
        "colony_create_commitment": {"description": "x"},
        "colony_initiative_feedback": {
            "initiative_id": "i-1", "action": "actioned",
        },
        "colony_record_insight": {"content": "x", "insight_type": "fact"},
        "colony_research": {"topic": "x"},
        "colony_resolve_commitment": {"commitment_id": "c-1"},
        "colony_task_complete": {"task_id": "t-1"},
        "colony_task_dismiss": {"task_id": "t-1"},
        "colony_task_snooze": {"task_id": "t-1"},
    }
    assert set(samples) == set(ACTION_TOOL_NAMES)
    for name, args in samples.items():
        display = TOOL_CATALOG[name].approval_display(args)
        assert set(display) == {"summary", "target", "risk"}
        assert all(
            isinstance(value, str) and value for value in display.values()
        )
