"""Truthful read-state contracts consumed by the Operator Deck."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException, Request
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.api.authority import legacy_authority


def _legacy_request() -> Request:
    request = Request({
        "type": "http", "method": "GET", "path": "/v1/host/projects",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("test", 80), "client": ("test", 1), "root_path": "",
    })
    request.state.colony_authority = legacy_authority()
    return request


class _FailingGoals:
    def list_goals(self, **_kwargs):
        raise RuntimeError("secret goals backend detail")


class _EmptyGoals:
    def list_goals(self, **_kwargs):
        return []


class _FailingProjects:
    def list_projects(self, **_kwargs):
        raise RuntimeError("secret projects backend detail")


class _EmptyProjects:
    def list_projects(self, **_kwargs):
        return []


@pytest.mark.asyncio
async def test_goals_not_wired_is_not_misreported_as_valid_empty(monkeypatch):
    monkeypatch.setattr(host, "_goals_store", None)

    with pytest.raises(HTTPException) as captured:
        await host.list_goals()

    assert captured.value.status_code == 501
    assert captured.value.detail == host._NOT_WIRED


@pytest.mark.asyncio
async def test_goals_failure_is_fixed_non_success_and_valid_empty_is_preserved(
    monkeypatch,
):
    monkeypatch.setattr(host, "_goals_store", _FailingGoals())
    with pytest.raises(HTTPException) as captured:
        await host.list_goals()
    assert captured.value.status_code == 500
    assert captured.value.detail == {
        "error": {
            "code": "goals_unavailable",
            "message": "Goals backend unavailable",
        },
    }
    assert "secret" not in str(captured.value.detail).lower()

    monkeypatch.setattr(host, "_goals_store", _EmptyGoals())
    value = await host.list_goals()
    assert value.model_dump() == {"goals": []}


@pytest.mark.asyncio
async def test_projects_failure_is_unavailable_and_valid_empty_is_preserved(
    monkeypatch,
):
    monkeypatch.setattr(
        host, "_project_engine", SimpleNamespace(store=_FailingProjects()),
    )
    failed = await host.list_projects(_legacy_request())
    assert failed == {
        "available": False,
        "reason": "projects_unavailable",
        "projects": [],
    }
    assert "secret" not in str(failed).lower()

    monkeypatch.setattr(
        host, "_project_engine", SimpleNamespace(store=_EmptyProjects()),
    )
    empty = await host.list_projects(_legacy_request())
    assert empty["available"] is True
    assert empty["count"] == 0
    assert empty["projects"] == []
    assert "error" not in empty


@pytest.mark.asyncio
async def test_projects_not_wired_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(host, "_project_engine", None)

    assert await host.list_projects(_legacy_request()) == {
        "available": False,
        "reason": "projects_not_wired",
        "projects": [],
    }
