"""Scoped-auth migration evidence and server-attested contact refresh."""

from __future__ import annotations

import json
import os
import stat

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.auth_telemetry import AuthTelemetry
from colony_sidecar.api.authority import KeyringError, load_keyring
from colony_sidecar.api.contact_grants import ContactGrantRegistry
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host


OWNER = "cid-owner"
SCOPED_SECRET = "scoped-admin-secret-with-enough-entropy"
LEGACY_SECRET = "legacy-migration-secret"


class _Graph:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.turn_calls: list[dict] = []

    async def recall(self, **kwargs):
        self.search_calls.append(kwargs)
        return []

    async def record_turn(self, **kwargs):
        self.turn_calls.append(kwargs)
        return "turn-memory"


def _principal(
    *,
    principal: str = "channel-context",
    secret: str = SCOPED_SECRET,
    scopes: list[str] | None = None,
    attested: dict | None = None,
) -> dict:
    value = {
        "principal": principal,
        "status": "active",
        "scopes": scopes or [
            "api:access", "auth:admin", "context:read", "memory:search",
            "turns:resolve-sender", "turns:write",
        ],
        "viewer_person_id": OWNER,
        "person_ids": [],
        "audiences": ["viewer", "owner"],
        "credentials": [{
            "id": "current",
            "secret": secret,
            "status": "active",
        }],
    }
    if attested is not None:
        value["attested_contact_grants"] = attested
        value["turn_ingress_platforms"] = list(attested.get("platforms") or [])
    return value


def _write_keyring(path, principals: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "principals": principals}))
    path.chmod(0o600)


def _headers(
    secret: str = SCOPED_SECRET,
    principal: str = "channel-context",
) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + secret,
        "X-Colony-Principal": principal,
    }


def _context_payload(person_id: str) -> dict:
    return {
        "identity": {"host_id": "test-host"},
        "context": {"session_id": "session-1", "contact_id": person_id},
        "incoming_message": {"role": "user", "content": "context probe"},
    }


@pytest.mark.asyncio
async def test_telemetry_distinguishes_legacy_scoped_denials_and_hides_material(
    tmp_path, monkeypatch,
):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal()])
    telemetry_path = tmp_path / "auth-telemetry.db"
    telemetry = AuthTelemetry(telemetry_path)
    grants = ContactGrantRegistry(tmp_path / "contact-grants.json")
    graph = _Graph()
    monkeypatch.setattr(host, "_graph", graph)
    monkeypatch.setenv("COLONY_API_KEY", LEGACY_SECRET)
    monkeypatch.setenv("COLONY_API_KEYRING_PATH", str(keyring))

    app = FastAPI()

    @app.get("/v1/host/private/{item_id}")
    async def private_item(item_id: str, request: Request):
        return {"ok": True, "principal": request.state.colony_authority.principal_id}

    app.add_middleware(
        ApiKeyMiddleware,
        api_key=LEGACY_SECRET,
        keyring_path=str(keyring),
        auth_telemetry=telemetry,
        contact_grants=grants,
    )
    app.include_router(host.router)

    concrete_private_id = "PRIVATE-customer-case-73919"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        scoped = await client.post(
            "/v1/host/memory/search",
            headers=_headers(),
            json={"identity": {"host_id": "test"}, "query": "alpha"},
        )
        denied_scope = await client.post(
            "/v1/host/memory/write",
            headers=_headers(),
            json={"identity": {"host_id": "test"}, "content": "no"},
        )
        mismatch_headers = _headers()
        mismatch_headers["X-Colony-Principal"] = "body-claimed-principal"
        mismatch = await client.post(
            "/v1/host/memory/search",
            headers=mismatch_headers,
            json={"identity": {"host_id": "test"}, "query": "alpha"},
        )
        legacy = await client.get(
            "/v1/host/goals",
            headers={"Authorization": "Bearer " + LEGACY_SECRET},
        )
        invalid = await client.get(
            "/v1/host/goals",
            headers={"Authorization": "Bearer attacker-supplied-value"},
        )
        dynamic = await client.get(
            "/v1/host/private/" + concrete_private_id,
            headers=_headers(),
        )
        admin = await client.get("/v1/host/admin/auth/status", headers=_headers())

    assert [
        scoped.status_code, denied_scope.status_code, mismatch.status_code,
        legacy.status_code, invalid.status_code, dynamic.status_code,
        admin.status_code,
    # The legacy principal passes authentication, then the intentionally
    # unwired goals backend reports its truthful 501 state.
    ] == [200, 403, 403, 501, 401, 200, 200]
    body = admin.json()
    assert body["secrets_exposed"] is False
    assert body["telemetry"]["totals"]["legacy_allow"] == 1
    assert body["telemetry"]["totals"]["scoped_allow"] >= 2
    assert body["telemetry"]["totals"]["deny"] == 3
    serialized = json.dumps(body, sort_keys=True)
    for forbidden in (
        SCOPED_SECRET,
        LEGACY_SECRET,
        "attacker-supplied-value",
        "body-claimed-principal",
        concrete_private_id,
    ):
        assert forbidden not in serialized
    assert "/v1/host/private/{item_id}" in serialized
    assert stat.S_IMODE(telemetry_path.stat().st_mode) == 0o600

    telemetry.close()
    reopened = AuthTelemetry(telemetry_path)
    try:
        persisted = reopened.snapshot()
        assert persisted["persistent"] is True
        assert persisted["totals"]["legacy_allow"] == 1
        assert persisted["totals"]["deny"] == 3
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_only_auth_admin_or_legacy_can_read_migration_status(tmp_path, monkeypatch):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [
        _principal(
            principal="ordinary-reader",
            secret="ordinary-reader-secret-with-enough-entropy",
            scopes=["api:access"],
        ),
    ])
    monkeypatch.setenv("COLONY_API_KEY", LEGACY_SECRET)
    monkeypatch.setenv("COLONY_API_KEYRING_PATH", str(keyring))
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=LEGACY_SECRET,
        keyring_path=str(keyring),
        auth_telemetry=AuthTelemetry(),
        contact_grants=ContactGrantRegistry(None),
    )
    app.include_router(host.router)
    ordinary = _headers(
        "ordinary-reader-secret-with-enough-entropy", "ordinary-reader",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        scoped_denied = await client.get(
            "/v1/host/admin/auth/status", headers=ordinary,
        )
        legacy_allowed = await client.get(
            "/v1/host/admin/auth/status",
            headers={"Authorization": "Bearer " + LEGACY_SECRET},
        )
    assert scoped_denied.status_code == 403
    assert legacy_allowed.status_code == 200


@pytest.mark.asyncio
async def test_server_resolver_projects_exact_contact_without_body_broadening(
    tmp_path, monkeypatch,
):
    class _EmptyProjection:
        facts = ()

    class _ContextRuntime:
        """Minimal attached visibility boundary for this auth-only test."""

        mode = "shadow"

        def projected_facts_view(self, *_args, **_kwargs):
            return None

        def project_shared_facts(self, *_args, **_kwargs):
            return _EmptyProjection()

    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(attested={
        "platforms": ["rcs"],
        "max_person_ids": 3,
    })])
    grants_path = tmp_path / "contact-grants.json"
    grants = ContactGrantRegistry(grants_path)
    graph = _Graph()
    monkeypatch.setattr(host, "_graph", graph)
    monkeypatch.setattr(host, "_p8_runtime", _ContextRuntime())
    monkeypatch.setattr(host, "_contacts_store", object())
    for name in (
        "_presence_store", "_context_provenance", "_telemetry",
        "_preference_learner", "_directive_manager", "_world_populator",
        "_tom_extractor", "_commitment_store",
    ):
        monkeypatch.setattr(host, name, None)

    from colony_sidecar.identity import participants
    from colony_sidecar.identity.participants import Resolution

    class _Resolver:
        def __init__(self, _store):
            pass

        async def resolve(self, *, platform, user_id, **_kwargs):
            return Resolution("cid-resolved-" + user_id, "handle")

    monkeypatch.setattr(participants, "ParticipantResolver", _Resolver)

    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        keyring_path=str(keyring),
        auth_telemetry=AuthTelemetry(),
        contact_grants=grants,
    )
    app.include_router(host.router)
    app.include_router(host.v2_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        readiness_before = await client.get(
            "/v1/host/context/projection-readiness",
            headers=_headers(),
            params={"contact_id": "cid-resolved-alice"},
        )
        before = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-resolved-alice"),
        )
        asserted_only = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-body-asserted"),
        )
        turn = await client.post(
            "/v1/host/turns/sync", headers=_headers(),
            json={
                "identity": {"host_id": "channel"},
                "context": {
                    "session_id": "s1",
                    "contact_id": "cid-body-asserted",
                    "channel_id": "rcs:thread-1",
                },
                "sender": {
                    "platform": "rcs",
                    "user_id": "alice",
                    "display_name": "Alice",
                },
                "summary": "attested turn",
            },
        )
        after = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-resolved-alice"),
        )
        readiness_after = await client.get(
            "/v1/host/context/projection-readiness",
            headers=_headers(),
            params={"contact_id": "cid-resolved-alice"},
        )
        readiness_asserted_only = await client.get(
            "/v1/host/context/projection-readiness",
            headers=_headers(),
            params={"contact_id": "cid-body-asserted"},
        )
        body_still_denied = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-body-asserted"),
        )
        disallowed_platform = await client.post(
            "/v1/host/turns/sync", headers=_headers(),
            json={
                "identity": {"host_id": "channel"},
                "context": {
                    "session_id": "s2",
                    "contact_id": "cid-body-email",
                    "channel_id": "email:thread-2",
                },
                "sender": {"platform": "email", "user_id": "mallory"},
                "summary": "wrong platform",
            },
        )
        email_context = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-resolved-mallory"),
        )

    assert readiness_before.status_code == 403
    assert before.status_code == 403
    assert asserted_only.status_code == 403
    assert turn.status_code == 200
    assert after.status_code == 200
    assert readiness_after.status_code == 200
    assert readiness_after.json() == {
        "schema": "ContextProjectionAttestationV1",
        "version": 1,
        "viewer_person_id": "cid-resolved-alice",
        "viewer_attested": True,
        "viewer_is_owner": False,
        "p8_mode": "shadow",
        "scoped_projection_ready": True,
        "legacy_global_allowed": False,
    }
    assert readiness_asserted_only.status_code == 403
    assert body_still_denied.status_code == 403
    assert disallowed_platform.status_code == 200
    assert email_context.status_code == 403
    assert graph.turn_calls[0]["contact_id"] == "cid-resolved-alice"
    assert stat.S_IMODE(grants_path.stat().st_mode) == 0o600
    assert grants.status()["principal_counts"] == {"channel-context": 1}

    # An owner-private atomic replacement hot-reloads exact IDs; it still
    # cannot add lanes or wildcard persons.
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({
        "version": 1,
        "principals": {
            "channel-context": {
                "person_ids": ["cid-resolved-alice", "cid-resolved-bob"],
                "updated_at": "2026-07-12T00:00:00+00:00",
            }
        },
    }))
    replacement.chmod(0o600)
    os.replace(replacement, grants_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bob = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-resolved-bob"),
        )
    assert bob.status_code == 200

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({
        "version": 1,
        "principals": {
            "channel-context": {"person_ids": ["*"], "updated_at": "now"}
        },
    }))
    invalid.chmod(0o600)
    os.replace(invalid, grants_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed_closed = await client.post(
            "/v1/host/context/assemble", headers=_headers(),
            json=_context_payload("cid-resolved-alice"),
        )
    assert failed_closed.status_code == 403
    assert grants.status()["error"]


def test_attested_policy_and_projection_are_exact_and_bounded(tmp_path):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(
        scopes=["turns:write"],
        attested={"platforms": ["rcs"], "max_person_ids": 2},
    )])
    with pytest.raises(KeyringError, match="requires turns:resolve-sender"):
        load_keyring(keyring)

    _write_keyring(keyring, [_principal(
        attested={"platforms": ["*"], "max_person_ids": 2},
    )])
    with pytest.raises(KeyringError, match="invalid platform"):
        load_keyring(keyring)

    wildcard = _principal(attested=None)
    wildcard["person_ids"] = ["cid-*"]
    _write_keyring(keyring, [wildcard])
    with pytest.raises(KeyringError, match="cannot contain a wildcard"):
        load_keyring(keyring)

    reserved = _principal(principal="legacy", attested=None)
    _write_keyring(keyring, [reserved])
    with pytest.raises(KeyringError, match="reserved telemetry identity"):
        load_keyring(keyring)

    registry = ContactGrantRegistry(tmp_path / "bounded-grants.json")
    assert registry.grant("channel-context", "cid-one", max_person_ids=1)
    assert not registry.grant("channel-context", "cid-two", max_person_ids=1)
    assert registry.status()["principal_counts"] == {"channel-context": 1}


def test_keyring_person_authority_ids_are_exact_and_bounded(tmp_path):
    keyring = tmp_path / "keys.json"

    for field, invalid in (
        ("principal", "p" * 129),
        ("viewer_person_id", "v" * 129),
    ):
        principal = _principal(attested=None)
        principal[field] = invalid
        _write_keyring(keyring, [principal])
        with pytest.raises(KeyringError):
            load_keyring(keyring)

    for field, invalid in (
        ("principal", " channel-context"),
        ("viewer_person_id", " cid-owner"),
    ):
        principal = _principal(attested=None)
        principal[field] = invalid
        _write_keyring(keyring, [principal])
        with pytest.raises(KeyringError, match="canonical"):
            load_keyring(keyring)

    for invalid in ("x" * 129, " cid-extra"):
        principal = _principal(attested=None)
        principal["person_ids"] = [invalid]
        _write_keyring(keyring, [principal])
        with pytest.raises(KeyringError, match="canonical"):
            load_keyring(keyring)

    principal = _principal(attested=None)
    principal["principal"] = "p" * 128
    principal["viewer_person_id"] = "v" * 128
    principal["person_ids"] = ["x" * 128]
    _write_keyring(keyring, [principal])
    loaded = load_keyring(keyring)
    assert loaded[0].principal_id == "p" * 128
    assert loaded[0].viewer_person_id == "v" * 128
    assert loaded[0].person_ids == frozenset({"x" * 128})


def test_websocket_auth_is_included_in_legacy_migration_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_API_KEY", LEGACY_SECRET)
    monkeypatch.delenv("COLONY_API_KEYRING_PATH", raising=False)
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    telemetry = AuthTelemetry()
    app = FastAPI()
    app.state.auth_telemetry = telemetry
    app.include_router(host.router)

    with TestClient(app) as client:
        with client.websocket_connect("/v1/host/events") as socket:
            socket.send_json({"type": "auth", "token": LEGACY_SECRET})
            assert socket.receive_json()["type"] == "connected"
            assert socket.receive_json()["type"] == "replay_complete"

    records = telemetry.snapshot()["records"]
    assert any(
        row["auth_kind"] == "legacy"
        and row["method"] == "WS"
        and row["route"] == "/v1/host/events"
        and row["decision"] == "allow"
        for row in records
    )


def test_doctor_reports_auth_migration_evidence(monkeypatch):
    from colony_sidecar import doctor

    healthy = {
        "auth": {"legacy_configured": False, "scoped_configured": True, "dual_accept": False},
        "telemetry": {
            "persistent": True,
            "error": None,
            "totals": {"legacy_allow": 0, "scoped_allow": 17, "deny": 2},
        },
        "keyring": {"configured": True, "available": True, "error": None},
        "contact_grants": {"error": None, "total_exact_person_ids": 8},
    }
    monkeypatch.setattr(doctor, "_http_get", lambda *_args: (200, healthy))
    passed = doctor.check_server_auth_migration("http://test", "key", 1)
    assert passed.status == doctor.PASS
    assert "scoped_allow=17" in passed.detail

    migrating = json.loads(json.dumps(healthy))
    migrating["auth"] = {
        "legacy_configured": True,
        "scoped_configured": True,
        "dual_accept": True,
    }
    migrating["telemetry"]["totals"]["legacy_allow"] = 9
    migrating["telemetry"]["principals"] = {
        "legacy": {
            "auth_kind": "legacy",
            "allow": 9,
            "deny": 0,
            "last_seen_at": "2099-07-12T00:00:00+00:00",
        }
    }
    monkeypatch.setattr(doctor, "_http_get", lambda *_args: (200, migrating))
    warned = doctor.check_server_auth_migration("http://test", "key", 1)
    assert warned.status == doctor.WARN
    assert "legacy bearer was used" in warned.detail
