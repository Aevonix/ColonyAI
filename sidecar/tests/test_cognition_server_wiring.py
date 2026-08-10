"""Startup wiring tests for the migration-gated P3 cognition spine."""

from types import SimpleNamespace

import pytest

from colony_sidecar.server import _attach_cognition_spine
from colony_sidecar.chain.node import get_or_create_node_id


class _ProjectStore:
    def __init__(self, planning=0, active=0):
        self.counts = {"planning": planning, "active": active}

    def count(self, status=None):
        return self.counts.get(status, 0)


def _dependencies(*, planning=0, active=0):
    project_store = _ProjectStore(planning=planning, active=active)
    return {
        "task_queue": SimpleNamespace(queue=object()),
        "workspace": SimpleNamespace(cognition_spine=None),
        "concern_store": object(),
        "project_store": project_store,
        "project_engine": SimpleNamespace(
            store=project_store,
            _work_orders=object(),
            open_capacity_used=lambda: (
                project_store.count("planning")
                + project_store.count("active")
            ),
        ),
        "directive_manager": SimpleNamespace(
            check=lambda _action: SimpleNamespace(
                allowed=True,
                reason="startup_probe_allowed",
            ),
        ),
    }


def test_default_off_does_not_create_database(tmp_path, monkeypatch):
    monkeypatch.delenv("COLONY_COGNITION_SPINE", raising=False)
    deps = _dependencies()

    result = _attach_cognition_spine(state_dir=tmp_path, **deps)

    assert result is None
    assert deps["workspace"].cognition_spine is None
    assert not (tmp_path / "colony-cognition.db").exists()


def test_enabled_spine_attaches_with_fixed_policy_config(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    monkeypatch.setenv("COLONY_PROJECTS_MAX_CONCURRENT", "2")
    monkeypatch.setenv(
        "COLONY_COGNITION_AVAILABLE_CAPABILITIES",
        "reasoning,memory:read,web:read",
    )
    deps = _dependencies(planning=1)

    spine = _attach_cognition_spine(state_dir=tmp_path, **deps)

    assert spine is deps["workspace"].cognition_spine
    assert spine._enforce_runtime_contract is True
    assert (tmp_path / "colony-cognition.db").exists()
    assert spine._available_capabilities == {
        "reasoning", "memory:read", "web:read",
    }
    proposal = SimpleNamespace(objective="Investigate", evidence_refs=("event:1",))
    assert spine._charter(proposal, object()) == (
        True, "typed_goal_with_source_evidence",
    )
    assert spine._situation(proposal, object()) == (
        True, "capacity_available",
    )
    deps["project_store"].counts["active"] = 1
    assert spine._situation(proposal, object()) == (
        False, "project_capacity_exhausted",
    )
    first_revision = spine._revision_provider()["situation_revision"]
    deps["project_store"].counts["active"] = 0
    second_revision = spine._revision_provider()["situation_revision"]
    assert first_revision != second_revision


@pytest.mark.parametrize("projects_mode", ["off", "shadow"])
def test_live_spine_rejects_non_live_project_engine(
    tmp_path, monkeypatch, projects_mode,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_PROJECTS_MODE", projects_mode)

    with pytest.raises(RuntimeError, match="ProjectEngine live mode"):
        _attach_cognition_spine(state_dir=tmp_path, **_dependencies())

    assert not (tmp_path / "colony-cognition.db").exists()


def test_live_spine_rejects_missing_work_order_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    deps = _dependencies()
    deps["project_engine"]._work_orders = None

    with pytest.raises(RuntimeError, match="WorkOrder adapter"):
        _attach_cognition_spine(state_dir=tmp_path, **deps)

    assert not (tmp_path / "colony-cognition.db").exists()


def test_live_spine_rejects_unreadable_directives(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    deps = _dependencies()

    def _failed_check(_action):
        raise RuntimeError("directive store unavailable")

    deps["directive_manager"].check = _failed_check

    with pytest.raises(RuntimeError, match="DirectiveGuard"):
        _attach_cognition_spine(state_dir=tmp_path, **deps)

    assert not (tmp_path / "colony-cognition.db").exists()


@pytest.mark.parametrize(
    "missing", [
        "task_queue", "workspace", "concern_store", "project_store",
        "project_engine", "directive_manager",
    ],
)
def test_enabled_spine_rejects_partial_attachment(
    tmp_path, monkeypatch, missing,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    deps = _dependencies()
    deps[missing] = None

    with pytest.raises(RuntimeError, match=missing):
        _attach_cognition_spine(state_dir=tmp_path, **deps)

    assert not (tmp_path / "colony-cognition.db").exists()


@pytest.mark.parametrize("value", ["zero", "0", "101", "-1"])
def test_enabled_spine_rejects_invalid_project_limit(
    tmp_path, monkeypatch, value,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    monkeypatch.setenv("COLONY_PROJECTS_MAX_CONCURRENT", value)

    with pytest.raises(RuntimeError, match="COLONY_PROJECTS_MAX_CONCURRENT"):
        _attach_cognition_spine(state_dir=tmp_path, **_dependencies())

    assert not (tmp_path / "colony-cognition.db").exists()


def _configure_thought_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    owner = get_or_create_node_id(tmp_path)
    monkeypatch.setenv("COLONY_THOUGHT_WORKER_NODE_ID", owner)
    return owner


def test_live_spine_rejects_missing_router(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    _configure_thought_owner(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="LLM router"):
        _attach_cognition_spine(
            state_dir=tmp_path,
            llm_router=None,
            embedded_worker_enabled=True,
            **_dependencies(),
        )


def test_live_spine_rejects_disabled_thought_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    _configure_thought_owner(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="embedded strict ThoughtJobV1"):
        _attach_cognition_spine(
            state_dir=tmp_path,
            llm_router=object(),
            embedded_worker_enabled=False,
            **_dependencies(),
        )


@pytest.mark.parametrize("configured", ["", "wrong-node"])
def test_live_spine_rejects_missing_or_wrong_thought_owner(
    tmp_path, monkeypatch, configured,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    if configured:
        monkeypatch.setenv("COLONY_THOUGHT_WORKER_NODE_ID", configured)
    else:
        monkeypatch.delenv("COLONY_THOUGHT_WORKER_NODE_ID", raising=False)
    with pytest.raises(RuntimeError, match="COLONY_THOUGHT_WORKER_NODE_ID"):
        _attach_cognition_spine(
            state_dir=tmp_path,
            llm_router=object(),
            embedded_worker_enabled=True,
            **_dependencies(),
        )


def test_live_spine_attaches_with_matching_production_thought_owner(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    _configure_thought_owner(tmp_path, monkeypatch)
    deps = _dependencies()
    spine = _attach_cognition_spine(
        state_dir=tmp_path,
        llm_router=object(),
        embedded_worker_enabled=True,
        **deps,
    )
    assert spine is deps["workspace"].cognition_spine
    assert spine.runtime_contract().requested_mode == "live"
    # Workspace defaults off in this isolated test, so attachment succeeds
    # but cognition is held instead of silently widening the mode lattice.
    assert spine.runtime_contract().effective_mode == "held"
