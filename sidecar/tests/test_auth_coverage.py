"""Every HTTP route must require auth when COLONY_API_KEY is set.

Auth is enforced by a single global ApiKeyMiddleware rather than per-route
Depends, so an endpoint cannot be accidentally left unauthenticated. This test
locks that guarantee in across the WHOLE route table (previously only 2 of ~117
endpoints had an explicit 401 assertion), so a future route can't regress it.
"""
import re

import pytest
from fastapi.testclient import TestClient


def _fill_params(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _api_paths(app):
    """All API paths from the OpenAPI schema (works across FastAPI versions)."""
    return sorted(app.openapi().get("paths", {}))


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("COLONY_API_KEY", "test-secret-key")
    from colony_sidecar.server import create_app
    return create_app()


def test_all_http_routes_reject_unauthenticated_requests(app):
    from colony_sidecar.api.middleware import _DEV_MODE_ALLOWED

    client = TestClient(app, raise_server_exceptions=False)
    checked = 0
    offenders = []
    for path in _api_paths(app):
        if path in _DEV_MODE_ALLOWED:
            continue
        url = _fill_params(path)
        resp = client.get(url)
        checked += 1
        if resp.status_code != 401:
            offenders.append((path, resp.status_code))

    assert checked > 50, f"expected to check the full route table, only saw {checked}"
    assert not offenders, f"routes reachable without auth: {offenders}"


def test_allowlisted_paths_do_not_require_auth(app):
    from colony_sidecar.api.middleware import _DEV_MODE_ALLOWED

    client = TestClient(app, raise_server_exceptions=False)
    for path in _DEV_MODE_ALLOWED:
        resp = client.get(path)
        assert resp.status_code != 401, f"{path} should be reachable without auth"


def test_valid_token_passes(app):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/v1/host/health", headers={"Authorization": "Bearer test-secret-key"})
    assert resp.status_code != 401
