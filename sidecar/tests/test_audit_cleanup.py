"""Tests for the audit-cleanup pass.

Covers the behaviours introduced by the April 2026 cleanup:
- IMAPProvider missing → email_reply condition returns cleanly.
- ApiKeyMiddleware refuses /v1/host/configure in dev mode.
- Skill route params reject invalid ids.
- Neo4j update_person rejects unknown property names.
- Skill AST scanner flags dunder-chain and dynamic-getattr escapes.
- Contact importer hashes PII and counts handle conflicts.
"""

from __future__ import annotations

import hashlib

import pytest

from colony_sidecar.autonomy import condition_worker
from colony_sidecar.contacts.importer import _pii_hash
from colony_sidecar.skills.security.scanner import ASTScanner


# ── A1: missing IMAPProvider is handled gracefully ────────────────────────────


@pytest.mark.asyncio
async def test_email_reply_without_imap_provider_returns_unavailable(monkeypatch):
    """If colony_sidecar.email.providers is missing, the condition checker
    must return a well-formed 'not met' result instead of raising."""

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "colony_sidecar.email.providers":
            raise ImportError("email module not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = await condition_worker._check_email_reply({})

    assert result["condition_met"] is False
    assert result["message_id"] is None
    assert result["from"] is None
    assert result["details"] == {"unavailable": "imap_provider_not_installed"}


# ── B2: /configure refused without COLONY_API_KEY ─────────────────────────────


@pytest.mark.asyncio
async def test_middleware_refuses_configure_in_dev_mode():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from colony_sidecar.api.middleware import ApiKeyMiddleware

    app = FastAPI()

    @app.post("/v1/host/configure")
    async def _configure():
        return {"ok": True}

    @app.get("/v1/host/health")
    async def _health():
        return {"ok": True}

    app.add_middleware(ApiKeyMiddleware, api_key=None)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/v1/host/health")
        assert health.status_code == 200

        configure = await client.post("/v1/host/configure", json={})
        assert configure.status_code == 503
        assert "COLONY_API_KEY" in configure.json()["detail"]


@pytest.mark.asyncio
async def test_middleware_accepts_valid_bearer():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from colony_sidecar.api.middleware import ApiKeyMiddleware

    app = FastAPI()

    @app.post("/v1/host/configure")
    async def _configure():
        return {"ok": True}

    app.add_middleware(ApiKeyMiddleware, api_key="s3cret")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthed = await client.post("/v1/host/configure", json={})
        assert unauthed.status_code == 401

        authed = await client.post(
            "/v1/host/configure",
            json={},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert authed.status_code == 200


# ── B3: skill_id validation ───────────────────────────────────────────────────


def test_skill_id_validator_accepts_safe_ids():
    from colony_sidecar.api.routers import host as host_mod

    for ok in ("skill_a", "skill-1", "alpha.beta", "S1"):
        host_mod._validate_skill_id(ok)  # should not raise


def test_skill_id_validator_rejects_unsafe_ids():
    from fastapi import HTTPException

    from colony_sidecar.api.routers import host as host_mod

    bad = ["../etc/passwd", "skill id", "a" * 100, "", "skill/evil", ".hidden"]
    for value in bad:
        with pytest.raises(HTTPException) as exc:
            host_mod._validate_skill_id(value)
        assert exc.value.status_code == 400


# ── B5: Neo4j property-name allowlist ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_person_rejects_unknown_properties():
    from colony_sidecar.intelligence.graph.client import ColonyGraph

    # Build a client instance without a real driver; the allowlist check
    # happens before any Cypher executes.
    client = ColonyGraph.__new__(ColonyGraph)
    client.driver = None
    client.database = "neo4j"

    with pytest.raises(ValueError) as exc:
        await client.update_person(
            "person-1",
            score=1.0,
            **{"name} SET p.admin = true; SET p.{": "x"},
        )
    assert "update_person rejected unknown" in str(exc.value)


@pytest.mark.asyncio
async def test_update_person_accepts_known_properties(monkeypatch):
    from colony_sidecar.intelligence.graph import client as client_mod

    executed = {}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def run(self, cypher, **params):
            executed["cypher"] = cypher
            executed["params"] = params
            return None

    class _FakeDriver:
        def session(self, database=None):
            return _FakeSession()

    client = client_mod.ColonyGraph.__new__(client_mod.ColonyGraph)
    client.driver = _FakeDriver()
    client.database = "neo4j"

    await client.update_person("p1", score=0.9, tier="bronze")
    assert "p.score = $score" in executed["cypher"]
    assert "p.tier = $tier" in executed["cypher"]
    assert executed["params"]["score"] == 0.9


# ── C1: AST scanner catches escape patterns ───────────────────────────────────


def test_scanner_flags_dunder_attribute_chain():
    src = (
        "def run():\n"
        "    return ().__class__.__bases__[0].__subclasses__()\n"
    )
    result = ASTScanner().scan(src, "skill-test")
    assert result.status == "critical"
    assert any(f.rule_id == "ESC001" for f in result.findings)


def test_scanner_flags_getattr_with_dunder_string():
    src = (
        "def run():\n"
        "    fn = getattr(__builtins__, '__import__')\n"
        "    return fn('os')\n"
    )
    result = ASTScanner().scan(src, "skill-test")
    assert result.status == "critical"
    assert any(f.rule_id == "ESC002" for f in result.findings)


def test_scanner_flags_dynamic_getattr():
    src = (
        "def run(name):\n"
        "    return getattr(__builtins__, name)\n"
    )
    result = ASTScanner().scan(src, "skill-test")
    assert result.status == "critical"
    assert any(f.rule_id == "ESC002" for f in result.findings)


def test_scanner_passes_plain_code():
    src = (
        "def run():\n"
        "    values = [1, 2, 3]\n"
        "    return sum(values)\n"
    )
    result = ASTScanner().scan(src, "skill-test")
    assert result.status == "clean"


# ── B4: PII hash is stable and short ──────────────────────────────────────────


def test_pii_hash_is_deterministic_and_short():
    value = "someone@example.com"
    h = _pii_hash(value)
    assert len(h) == 8
    expected = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    assert h == expected


def test_pii_hash_handles_empty():
    assert _pii_hash(None) == "∅"
    assert _pii_hash("") == "∅"
