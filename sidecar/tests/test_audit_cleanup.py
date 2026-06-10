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


# ── Wizard: Neo4j password auto-generation ────────────────────────────────────


def test_setup_wizard_has_no_shared_default_password():
    """Regression guard — the old 'colony-local-dev' shared default must stay
    out of setup.py and docker-compose.yml."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    setup_src = (repo / "sidecar/colony_sidecar/setup.py").read_text()
    compose_src = (repo / "docker-compose.yml").read_text()
    assert "colony-local-dev" not in setup_src
    assert "colony-local-dev" not in compose_src


def test_start_neo4j_docker_forwards_password(monkeypatch):
    """_start_neo4j_docker must pass the credential via the process env —
    never in argv, where `ps` exposes it to any local user."""
    import subprocess
    from colony_sidecar import setup as wizard

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, capture_output=None, text=None, timeout=None, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = wizard._start_neo4j_docker("super-secret-abc")
    assert ok is True
    assert captured["env"]["NEO4J_AUTH"] == "neo4j/super-secret-abc"
    assert "super-secret-abc" not in " ".join(captured["cmd"])


def test_env_roundtrip_preserves_generated_password(tmp_path):
    """A generated password written via _write_env must come back through
    _load_existing_env byte-for-byte, including URL-safe special chars."""
    import secrets
    from colony_sidecar.setup import _load_existing_env, _write_env

    generated = secrets.token_urlsafe(24)
    env_path = tmp_path / ".env"
    _write_env(env_path, {"NEO4J_PASSWORD": generated, "COLONY_API_KEY": "k"})

    loaded = _load_existing_env(env_path)
    assert loaded["NEO4J_PASSWORD"] == generated
    assert loaded["COLONY_API_KEY"] == "k"


# ── Rate limiter persistence ──────────────────────────────────────────────────


def test_rate_limiter_in_memory_default_still_works():
    """Backwards compat: no db_path means pure in-memory (existing behavior)."""
    from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter

    # Disable quiet hours so the test doesn't depend on the wall clock
    # (default quiet hours made this fail when the suite ran at night).
    rl = DeliveryRateLimiter(quiet_start_hour=0, quiet_end_hour=0)
    ok, _ = rl.can_deliver("alice")
    assert ok is True
    rl.record_delivery("alice")
    assert rl.daily_count("alice") == 1


def test_rate_limiter_persists_count_across_restart(tmp_path, monkeypatch):
    """Record 2 deliveries, 'restart' by constructing a fresh limiter on the
    same db, and confirm the count survives and the daily limit is enforced."""
    from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter

    # Force a non-quiet UTC hour so the deliveries are allowed.
    db = tmp_path / "delivery.db"
    rl1 = DeliveryRateLimiter(
        db_path=db, quiet_start_hour=0, quiet_end_hour=0,
        cooldown_hours=0,
    )
    rl1.record_delivery("alice")
    rl1.record_delivery("alice")
    assert rl1.daily_count("alice") == 2

    # Simulate a restart.
    rl2 = DeliveryRateLimiter(
        db_path=db, quiet_start_hour=0, quiet_end_hour=0,
        cooldown_hours=0,
    )
    assert rl2.daily_count("alice") == 2

    # Third delivery is still allowed (limit is 3).
    ok, _ = rl2.can_deliver("alice")
    assert ok is True
    rl2.record_delivery("alice")

    # Fourth would exceed the cap — also after a restart.
    rl3 = DeliveryRateLimiter(
        db_path=db, quiet_start_hour=0, quiet_end_hour=0,
        cooldown_hours=0,
    )
    ok, reason = rl3.can_deliver("alice")
    assert ok is False
    assert "daily_limit_reached" in reason


def test_rate_limiter_cooldown_restored_from_db(tmp_path):
    """Cooldown based on last delivery must survive a restart."""
    from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter

    db = tmp_path / "delivery.db"
    rl1 = DeliveryRateLimiter(
        db_path=db, quiet_start_hour=0, quiet_end_hour=0,
        cooldown_hours=2,
    )
    rl1.record_delivery("alice")

    rl2 = DeliveryRateLimiter(
        db_path=db, quiet_start_hour=0, quiet_end_hour=0,
        cooldown_hours=2,
    )
    ok, reason = rl2.can_deliver("alice")
    assert ok is False
    assert "cooldown_active" in reason


def test_rate_limiter_persistence_failure_falls_back_to_memory(tmp_path, caplog):
    """If the db path is unusable, the limiter must still work in-memory."""
    from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter

    # Point at a path whose parent cannot be created — pass a file as the
    # parent directory.
    bogus = tmp_path / "i-am-a-file"
    bogus.write_text("x")
    db = bogus / "delivery.db"  # parent is a file, not a dir

    rl = DeliveryRateLimiter(
        db_path=db, quiet_start_hour=0, quiet_end_hour=0, cooldown_hours=0,
    )
    # Should not raise, and operate in-memory.
    rl.record_delivery("alice")
    assert rl.daily_count("alice") == 1


# ── Body size middleware ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_body_size_middleware_rejects_oversized_payload():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from colony_sidecar.api.middleware import BodySizeLimitMiddleware

    app = FastAPI()

    @app.post("/echo")
    async def _echo(body: dict):
        return body

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=64)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        small = await client.post("/echo", json={"a": 1})
        assert small.status_code == 200

        big = await client.post("/echo", content=b"x" * 128)
        assert big.status_code == 413
        assert "exceeds limit" in big.json()["detail"]


@pytest.mark.asyncio
async def test_body_size_middleware_allows_missing_content_length():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from colony_sidecar.api.middleware import BodySizeLimitMiddleware

    app = FastAPI()

    @app.get("/ping")
    async def _ping():
        return {"ok": True}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=64)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/ping")
        assert resp.status_code == 200
