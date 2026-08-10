"""Exact authority contract for the read-only operator work surface."""

from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.auth_telemetry import AuthTelemetry
from colony_sidecar.api.authority import (
    WORK_READ_SURFACE_V1,
    compatible_scopes,
    required_scope,
)
from colony_sidecar.api.middleware import ApiKeyMiddleware


_EXPECTED_SURFACE = frozenset({
    ("GET", "/v1/host/goals"),
    ("GET", "/v1/host/projects"),
    ("GET", "/v1/host/queue/jobs/pending"),
    ("GET", "/v1/host/queue/jobs/neutral"),
})

_ADJACENT_ROUTES = (
    ("GET", "/v1/host/goals/{goal_id}", "/v1/host/goals/goal-1"),
    ("GET", "/v1/host/projects/{project_id}", "/v1/host/projects/project-1"),
    ("GET", "/v1/host/queue/jobs/completed", "/v1/host/queue/jobs/completed"),
    ("POST", "/v1/host/goals", "/v1/host/goals"),
    ("POST", "/v1/host/projects", "/v1/host/projects"),
    ("POST", "/v1/host/queue/jobs", "/v1/host/queue/jobs"),
    (
        "POST",
        "/v1/host/queue/work/operations",
        "/v1/host/queue/work/operations",
    ),
    ("POST", "/v1/host/memory/search", "/v1/host/memory/search"),
)


async def _admitted() -> dict:
    return {"ok": True}


def _write_keyring(
    path,
    *,
    scopes: list[str],
    allow_unscoped_api: bool,
) -> None:
    path.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "operator-reader",
            "status": "active",
            "scopes": scopes,
            "allow_unscoped_api": allow_unscoped_api,
            "viewer_person_id": "owner-person",
            "person_ids": ["owner-person"],
            "audiences": ["owner"],
            "credentials": [{
                "id": "current",
                "secret": "reader-secret",
                "status": "active",
            }],
        }],
    }))
    path.chmod(0o600)


def _app(
    keyring_path,
    *,
    legacy_key: str | None = None,
    telemetry: AuthTelemetry | None = None,
) -> FastAPI:
    app = FastAPI()
    for index, (method, path) in enumerate(sorted(_EXPECTED_SURFACE)):
        app.add_api_route(
            path,
            _admitted,
            methods=[method],
            name=f"work-read-{index}",
        )
    for index, (method, route, _request_path) in enumerate(_ADJACENT_ROUTES):
        app.add_api_route(
            route,
            _admitted,
            methods=[method],
            name=f"adjacent-{index}",
        )
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=legacy_key,
        keyring_path=str(keyring_path) if keyring_path else None,
        auth_telemetry=telemetry,
    )
    return app


def _surface_url(path: str) -> str:
    if path == "/v1/host/goals":
        return f"{path}?status_filter=active"
    if path == "/v1/host/projects":
        return f"{path}?limit=30"
    return f"{path}?limit=100"


async def _read_surface(client: AsyncClient, secret: str) -> list:
    return [
        await client.get(
            _surface_url(path),
            headers={"Authorization": f"Bearer {secret}"},
        )
        for _method, path in sorted(_EXPECTED_SURFACE)
    ]


def test_work_read_surface_v1_is_exact_and_route_local():
    assert WORK_READ_SURFACE_V1 == _EXPECTED_SURFACE
    for method, path in sorted(_EXPECTED_SURFACE):
        assert required_scope(method, path) == "work:read"
        assert compatible_scopes(method, path) == frozenset({"api:access"})

    adjacent = (
        ("GET", "/v1/host/goals/goal-1"),
        ("GET", "/v1/host/projects/project-1"),
        ("GET", "/v1/host/queue/jobs/completed"),
        ("POST", "/v1/host/goals"),
        ("POST", "/v1/host/projects"),
        ("POST", "/v1/host/queue/jobs"),
        ("GET", "/v1/host/queue/work/job-1"),
        ("POST", "/v1/host/queue/work/operations"),
        ("POST", "/v1/host/memory/search"),
    )
    for method, path in adjacent:
        assert compatible_scopes(method, path) == frozenset()

    assert required_scope("GET", "/v1/host/goals/goal-1") == "api:access"
    assert required_scope("POST", "/v1/host/projects") == "api:access"
    assert required_scope("GET", "/v1/host/queue/work/job-1") == "work:read"
    assert required_scope(
        "POST", "/v1/host/queue/work/operations",
    ) == "work:control"


@pytest.mark.asyncio
async def test_exact_work_read_principal_with_unscoped_disabled_reads_all_four(
    tmp_path,
):
    keyring = tmp_path / "keyring.json"
    _write_keyring(
        keyring, scopes=["work:read"], allow_unscoped_api=False,
    )
    app = _app(keyring)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = await _read_surface(client, "reader-secret")

    assert [response.status_code for response in responses] == [200] * 4


@pytest.mark.asyncio
async def test_api_access_compatibility_preserves_existing_scoped_readers(
    tmp_path,
):
    keyring = tmp_path / "keyring.json"
    _write_keyring(
        keyring, scopes=["api:access"], allow_unscoped_api=True,
    )
    telemetry = AuthTelemetry()
    app = _app(keyring, telemetry=telemetry)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = await _read_surface(client, "reader-secret")

    assert [response.status_code for response in responses] == [200] * 4
    records = telemetry.snapshot()["records"]
    assert len(records) == 4
    assert {record["required_scope"] for record in records} == {"work:read"}
    assert {record["reason"] for record in records} == {
        "compatible_scope_allowed"
    }


@pytest.mark.asyncio
async def test_api_access_compatibility_obeys_unscoped_disable(tmp_path):
    keyring = tmp_path / "keyring.json"
    _write_keyring(
        keyring, scopes=["api:access"], allow_unscoped_api=False,
    )
    app = _app(keyring)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = await _read_surface(client, "reader-secret")

    assert [response.status_code for response in responses] == [403] * 4
    assert {
        response.json()["detail"]["code"] for response in responses
    } == {"unscoped_api_denied"}


@pytest.mark.asyncio
async def test_exact_scope_wins_when_unscoped_compatibility_is_disabled(tmp_path):
    keyring = tmp_path / "keyring.json"
    _write_keyring(
        keyring,
        scopes=["work:read", "api:access"],
        allow_unscoped_api=False,
    )
    app = _app(keyring)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = await _read_surface(client, "reader-secret")

    assert [response.status_code for response in responses] == [200] * 4


@pytest.mark.asyncio
async def test_work_read_scope_cannot_reach_adjacent_or_mutating_routes(tmp_path):
    keyring = tmp_path / "keyring.json"
    _write_keyring(
        keyring, scopes=["work:read"], allow_unscoped_api=False,
    )
    app = _app(keyring)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = [
            await client.request(
                method,
                request_path,
                headers={"Authorization": "Bearer reader-secret"},
            )
            for method, _route, request_path in _ADJACENT_ROUTES
        ]

    assert [response.status_code for response in responses] == [403] * len(
        _ADJACENT_ROUTES
    )
    assert {
        response.json()["detail"]["code"] for response in responses
    } == {"unscoped_api_denied", "insufficient_scope"}


@pytest.mark.asyncio
async def test_legacy_bearer_keeps_existing_work_read_surface_access(tmp_path):
    app = _app(None, legacy_key="legacy-secret")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        responses = await _read_surface(client, "legacy-secret")

    assert [response.status_code for response in responses] == [200] * 4
