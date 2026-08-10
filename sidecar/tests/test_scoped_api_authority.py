"""Scoped API principals own person/audience authority at the HTTP boundary.

These tests reproduce the pre-slice failure: possession of the one legacy
bearer let any client put an arbitrary ``person_id`` in a memory or turn body.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.contact_grants import ContactGrantRegistry
from colony_sidecar.api.authority import KeyringError, load_keyring
from colony_sidecar.api.routers import host


class _Graph:
    def __init__(self) -> None:
        self.read_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.write_calls: list[dict] = []
        self.turn_calls: list[dict] = []

    async def read_memories(self, **kwargs):
        self.read_calls.append(kwargs)
        return []

    async def recall(self, **kwargs):
        self.search_calls.append(kwargs)
        return []

    async def store_memory(self, **kwargs):
        self.write_calls.append(kwargs)
        return "memory-1"

    async def record_turn(self, **kwargs):
        self.turn_calls.append(kwargs)
        return "turn-memory-1"


def _principal(
    *,
    principal: str = "hermes-text",
    secret: str = "scoped-secret",
    scopes: list[str] | None = None,
    viewer: str = "contact-owner",
    audiences: list[str] | None = None,
    status: str = "active",
    accept_until: str | None = None,
) -> dict:
    granted_scopes = scopes or [
        "memory:read", "memory:search", "memory:write", "context:read",
        "turns:write"
    ]
    value = {
        "principal": principal,
        "status": status,
        "scopes": granted_scopes,
        "viewer_person_id": viewer,
        "audiences": audiences or ["viewer"],
        "credentials": [
            {"id": "current", "secret": secret, "status": "active"}
        ],
    }
    if "turns:write" in granted_scopes or "*" in granted_scopes:
        value["turn_ingress_platforms"] = ["voice"]
    if accept_until is not None:
        value["accept_until"] = accept_until
    return value


def _write_keyring(path, principals: list[dict], *, mode: int = 0o600) -> None:
    path.write_text(json.dumps({"version": 1, "principals": principals}))
    path.chmod(mode)


def _app(
    keyring_path,
    *,
    legacy_key: str | None = None,
    contact_grants: ContactGrantRegistry | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=legacy_key,
        keyring_path=str(keyring_path) if keyring_path else None,
        contact_grants=contact_grants,
    )
    app.include_router(host.router)
    app.include_router(host.v2_router)
    return app


def _headers(secret: str, principal: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {secret}"}
    if principal is not None:
        headers["X-Colony-Principal"] = principal
    return headers


def _memory_payload(**extra) -> dict:
    payload = {"identity": {"host_id": "test-host"}}
    payload.update(extra)
    return payload


def _turn_payload(contact_id: str, turn_id: str = "authority-turn") -> dict:
    return {
        "identity": {"host_id": "test-host"},
        "context": {
            "session_id": "session-1",
            "contact_id": contact_id,
            "channel_id": "test:thread-1",
            "turn_id": turn_id,
        },
        "summary": "Authority-bound turn",
    }


@pytest.mark.parametrize(
    ("platforms", "scopes"),
    [(["Voice"], ["turns:write"]), (["*"], ["turns:write"]),
     (["voice"], ["memory:read"])]
)
def test_turn_ingress_platform_role_is_exact_and_requires_turn_write(
    tmp_path, platforms, scopes,
):
    principal = _principal(scopes=scopes)
    principal["turn_ingress_platforms"] = platforms
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    with pytest.raises(KeyringError):
        load_keyring(keyring)


def test_attested_contact_platforms_must_be_ingress_role_subset(tmp_path):
    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["turn_ingress_platforms"] = ["voice"]
    principal["attested_contact_grants"] = {
        "platforms": ["rcs"], "max_person_ids": 1,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    with pytest.raises(KeyringError):
        load_keyring(keyring)


def test_legacy_turn_writer_derives_ingress_only_from_static_contact_policy(
    tmp_path,
):
    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal.pop("turn_ingress_platforms")
    principal["attested_contact_grants"] = {
        "platforms": ["rcs", "sms"], "max_person_ids": 2,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])

    loaded = load_keyring(keyring)[0]

    assert loaded.turn_ingress_platforms == frozenset({"rcs", "sms"})


def test_explicit_empty_ingress_role_does_not_use_compatibility_derivation(
    tmp_path,
):
    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["turn_ingress_platforms"] = []
    principal["attested_contact_grants"] = {
        "platforms": ["rcs"], "max_person_ids": 1,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])

    with pytest.raises(KeyringError, match="must be a subset"):
        load_keyring(keyring)


def test_senderless_legacy_turn_writer_without_static_policy_stays_ineligible(
    tmp_path,
):
    principal = _principal(scopes=["turns:write"])
    principal.pop("turn_ingress_platforms")
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])

    assert load_keyring(keyring)[0].turn_ingress_platforms == frozenset()


@pytest.fixture
def graph(monkeypatch, tmp_path):
    graph = _Graph()
    monkeypatch.setattr(host, "_graph", graph)
    monkeypatch.setattr(host, "_presence_store", None)
    monkeypatch.setattr(host, "_contacts_store", None)
    monkeypatch.setattr(host, "_context_provenance", None)
    monkeypatch.setattr(host, "_telemetry", None)
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "contact-owner")
    monkeypatch.setenv("COLONY_SHARED_PERSON_ID", "audience-shared")
    monkeypatch.setenv("COLONY_GLOBAL_PERSON_ID", "audience-global")
    monkeypatch.setenv("COLONY_DEV_PERSON_ID", "dev-anonymous")
    return graph


@pytest.mark.asyncio
async def test_legacy_and_scoped_tokens_are_accepted_during_migration(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["memory:search"])])
    app = _app(keyring, legacy_key="legacy-secret")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        scoped = await c.post(
            "/v1/host/memory/search",
            headers=_headers("scoped-secret", "hermes-text"),
            json=_memory_payload(query="alpha"),
        )
        legacy = await c.post(
            "/v1/host/memory/search",
            headers=_headers("legacy-secret"),
            json=_memory_payload(query="alpha", person_id="legacy-contact"),
        )

    assert scoped.status_code == 200
    assert legacy.status_code == 200
    assert [call["person_id"] for call in graph.search_calls] == [
        "contact-owner", "legacy-contact"
    ]


@pytest.mark.asyncio
async def test_scoped_token_is_denied_without_exact_route_scope(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["memory:read"])])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/memory/write",
            headers=_headers("scoped-secret"),
            json=_memory_payload(content="private fact"),
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    assert graph.write_calls == []


@pytest.mark.asyncio
async def test_query_person_selector_cannot_broaden_scoped_principal(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["api:access"])])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        allowed = await c.get(
            "/v1/host/goals", headers=_headers("scoped-secret"),
            params={"person_id": "contact-owner"},
        )
        broadened = await c.get(
            "/v1/host/goals", headers=_headers("scoped-secret"),
            params={"person_id": "someone-else"},
        )
    # Exact contact scope passes middleware; the unwired goals backend then
    # reports a truthful non-success rather than a fabricated empty result.
    assert allowed.status_code == 501
    assert broadened.status_code == 403
    assert broadened.json()["detail"]["code"] == "person_scope_not_granted"


@pytest.mark.asyncio
async def test_claimed_principal_header_must_match_token(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["memory:search"])])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/memory/search",
            headers=_headers("scoped-secret", "spoofed-principal"),
            json=_memory_payload(query="alpha"),
        )
    assert response.status_code == 403
    assert graph.search_calls == []


@pytest.mark.asyncio
async def test_keyring_requires_private_permissions_but_legacy_still_works(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal()], mode=0o644)
    app = _app(keyring, legacy_key="legacy-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        scoped = await c.post(
            "/v1/host/memory/search",
            headers=_headers("scoped-secret"),
            json=_memory_payload(query="alpha"),
        )
        legacy = await c.post(
            "/v1/host/memory/search",
            headers=_headers("legacy-secret"),
            json=_memory_payload(query="alpha", person_id="legacy-contact"),
        )
    assert scoped.status_code == 401
    assert legacy.status_code == 200


@pytest.mark.asyncio
async def test_permission_change_invalidates_loaded_scoped_credentials(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["memory:search"])])
    app = _app(keyring, legacy_key="legacy-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        before = await c.post(
            "/v1/host/memory/search", headers=_headers("scoped-secret"),
            json=_memory_payload(query="alpha"),
        )
        keyring.chmod(0o644)
        after = await c.post(
            "/v1/host/memory/search", headers=_headers("scoped-secret"),
            json=_memory_payload(query="alpha"),
        )
    assert before.status_code == 200
    assert after.status_code == 401


@pytest.mark.asyncio
async def test_expired_or_revoked_principal_is_rejected(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _write_keyring(keyring, [
        _principal(principal="expired", secret="expired-key", accept_until=expired),
        _principal(principal="revoked", secret="revoked-key", status="revoked"),
    ])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        expired_response = await c.post(
            "/v1/host/memory/search", headers=_headers("expired-key"),
            json=_memory_payload(query="alpha"),
        )
        revoked_response = await c.post(
            "/v1/host/memory/search", headers=_headers("revoked-key"),
            json=_memory_payload(query="alpha"),
        )
    assert expired_response.status_code == 401
    assert revoked_response.status_code == 401


@pytest.mark.asyncio
async def test_keyring_reloads_after_atomic_replacement(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(secret="first-key", scopes=["memory:search"])])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = await c.post(
            "/v1/host/memory/search", headers=_headers("first-key"),
            json=_memory_payload(query="one"),
        )
        replacement = tmp_path / "replacement.json"
        _write_keyring(
            replacement,
            [_principal(secret="second-key", scopes=["memory:search"])],
        )
        os.replace(replacement, keyring)
        old = await c.post(
            "/v1/host/memory/search", headers=_headers("first-key"),
            json=_memory_payload(query="old"),
        )
        new = await c.post(
            "/v1/host/memory/search", headers=_headers("second-key"),
            json=_memory_payload(query="new"),
        )
    assert (first.status_code, old.status_code, new.status_code) == (200, 401, 200)


@pytest.mark.asyncio
async def test_memory_person_is_derived_and_body_cannot_broaden_it(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal()])
    app = _app(keyring)
    headers = _headers("scoped-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        read = await c.post(
            "/v1/host/memory/read", headers=headers, json=_memory_payload()
        )
        search = await c.post(
            "/v1/host/memory/search", headers=headers,
            json=_memory_payload(query="alpha"),
        )
        write = await c.post(
            "/v1/host/memory/write", headers=headers,
            json=_memory_payload(content="private fact"),
        )
        broaden = await c.post(
            "/v1/host/memory/search", headers=headers,
            json=_memory_payload(query="alpha", person_id="someone-else"),
        )

    assert (read.status_code, search.status_code, write.status_code) == (200, 200, 200)
    assert broaden.status_code == 403
    assert graph.read_calls[0]["person_id"] == "contact-owner"
    assert graph.search_calls[0]["person_id"] == "contact-owner"
    assert graph.write_calls[0]["person_id"] == "contact-owner"


@pytest.mark.asyncio
async def test_memory_context_and_person_claim_must_agree(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal()])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/memory/write",
            headers=_headers("scoped-secret"),
            json=_memory_payload(
                content="private fact",
                person_id="contact-owner",
                context={"session_id": "s1", "contact_id": "someone-else"},
            ),
        )
    assert response.status_code == 403
    assert graph.write_calls == []


@pytest.mark.asyncio
async def test_enriched_context_uses_authenticated_viewer_not_body_claim(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["context:read"])])
    app = _app(keyring)
    base = {
        "identity": {"host_id": "test-host"},
        "message": "alpha",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        valid = await c.post(
            "/v1/host/context/enriched", headers=_headers("scoped-secret"),
            json={
                **base,
                "context": {"session_id": "s1", "contact_id": "contact-owner"},
            },
        )
        spoofed = await c.post(
            "/v1/host/context/enriched", headers=_headers("scoped-secret"),
            json={
                **base,
                "context": {"session_id": "s2", "contact_id": "someone-else"},
            },
        )

    assert valid.status_code == 200
    assert spoofed.status_code == 403
    assert graph.search_calls[0]["person_id"] == "contact-owner"


@pytest.mark.asyncio
async def test_owner_shared_global_lanes_are_explicit_and_single_scoped(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [
        _principal(audiences=["viewer", "owner", "shared", "global"])
    ])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for audience in ("owner", "shared", "global"):
            response = await c.post(
                "/v1/host/memory/search", headers=_headers("scoped-secret"),
                json=_memory_payload(query="alpha", audience=audience),
            )
            assert response.status_code == 200

    assert [call["person_id"] for call in graph.search_calls] == [
        "contact-owner", "audience-shared", "audience-global"
    ]
    assert all(call["person_id"] is not None for call in graph.search_calls)


@pytest.mark.asyncio
async def test_ungranted_audience_lane_is_rejected(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(audiences=["viewer"])])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/memory/search", headers=_headers("scoped-secret"),
            json=_memory_payload(query="alpha", audience="global"),
        )
    assert response.status_code == 403
    assert graph.search_calls == []


@pytest.mark.asyncio
async def test_anonymous_dev_mode_never_gets_reserved_authority(graph):
    app = _app(None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        ordinary = await c.post(
            "/v1/host/memory/search",
            json=_memory_payload(query="alpha", person_id="local-scratch"),
        )
        derived = await c.post(
            "/v1/host/memory/search", json=_memory_payload(query="alpha")
        )
        owner = await c.post(
            "/v1/host/memory/search",
            json=_memory_payload(query="alpha", person_id="contact-owner"),
        )
        global_lane = await c.post(
            "/v1/host/memory/search",
            json=_memory_payload(query="alpha", audience="global"),
        )
    assert ordinary.status_code == 200
    assert derived.status_code == 200
    assert owner.status_code == 403
    assert global_lane.status_code == 403
    assert [call["person_id"] for call in graph.search_calls] == [
        "local-scratch", "dev-anonymous"
    ]


@pytest.mark.asyncio
async def test_turn_contact_is_validated_before_idempotent_ingestion(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal()])
    app = _app(keyring)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        valid = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"),
            json=_turn_payload("contact-owner", "valid-turn"),
        )
        spoofed = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"),
            json=_turn_payload("someone-else", "spoofed-turn"),
        )
        v2_body = _turn_payload("someone-else", "unused-body-turn")
        v2_body["context"].pop("turn_id")
        spoofed_v2 = await c.put(
            "/v2/host/turns/spoofed-v2", headers=_headers("scoped-secret"),
            json=v2_body,
        )

    assert valid.status_code == 200
    assert spoofed.status_code == 403
    assert spoofed_v2.status_code == 403
    assert [call["contact_id"] for call in graph.turn_calls] == ["contact-owner"]


@pytest.mark.asyncio
async def test_sender_resolving_adapter_discards_initial_contact_claim(tmp_path, graph):
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=[
        "turns:write", "turns:resolve-sender"
    ])])
    app = _app(keyring)
    payload = _turn_payload("body-claim", "sender-turn")
    payload["sender"] = {
        "platform": "example-chat",
        "user_id": "transport-user-7",
        "display_name": "Example",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    # No contacts store is wired in this focused test, so the resolver cannot
    # map the sender further. The untrusted body claim is still gone.
    assert graph.turn_calls[0]["contact_id"] == "contact-owner"


@pytest.mark.asyncio
async def test_turn_concern_journal_scope_is_sealed_from_scoped_authority(
    tmp_path, graph, monkeypatch,
):
    from colony_sidecar.events import journal as event_journal

    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["turns:write"])])
    app = _app(keyring)
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("contact-owner", "scoped-turn-concern")
    payload["context"]["channel_id"] = "voice:call-1"
    payload["context"]["metadata"] = {
        "identity_attested": False,
        "scope_attested": False,
        "subject_person_id": "someone-else",
        "viewer_scope": "public",
        "shareability": "public",
        "boundary_attested": True,
        "source_platform": "rcs",
        "source_platform_attested": True,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    assert len(recorded) == 1
    event_type, data = recorded[0]
    assert event_type == "conversation.turn"
    assert data["turn_scope_schema"] == "ConversationTurnJournalScopeV1"
    assert data["turn_id"] == "scoped-turn-concern"
    assert data["contact_id"] == data["subject_person_id"] == "contact-owner"
    assert data["identity_attested"] is True
    assert data["scope_attested"] is True
    assert data["attribution_method"] == "authority_binding"
    assert data["source_principal_id"] == "hermes-text"
    assert data["source_platform"] == "voice"
    assert data["source_platform_attested"] is True
    assert data["viewer_scope"] == "owner"
    assert data["shareability"] == "owner_private"
    assert data["boundary_attested"] is False


@pytest.mark.asyncio
async def test_scoped_unkeyed_turn_gets_deterministic_server_lineage_id(
    tmp_path, graph, monkeypatch,
):
    from colony_sidecar.events import journal as event_journal

    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["turns:write"])])
    app = _app(keyring)
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("contact-owner", "unused")
    payload["context"].pop("turn_id")
    payload["context"]["session_id"] = "call-owner-0001"
    payload["context"]["channel_id"] = "voice"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )
        retry = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )
        changed_payload = json.loads(json.dumps(payload))
        changed_payload["summary"] = "A different completed voice turn"
        changed = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=changed_payload,
        )

    assert first.status_code == retry.status_code == changed.status_code == 200
    ids = [entry[1]["turn_id"] for entry in recorded]
    assert ids[0] == ids[1]
    assert ids[0].startswith("server-turn-")
    assert ids[2] != ids[0]
    assert all(entry[1]["turn_id_source"] == "server_digest" for entry in recorded)
    assert all(entry[1]["turn_id_attested"] is True for entry in recorded)


@pytest.mark.asyncio
async def test_server_resolved_attested_sender_gets_subject_private_turn_scope(
    tmp_path, graph, monkeypatch,
):
    from types import SimpleNamespace
    from colony_sidecar.events import journal as event_journal

    class Contacts:
        async def resolve_messaging_handle(self, platform, user_id):
            assert (platform, user_id) == ("voice", "transport-speaker-7")
            return SimpleNamespace(contact_id="contact-guest")

        async def record_interaction(self, _contact_id):
            return None

        async def get(self, _contact_id):
            return None

    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["attested_contact_grants"] = {
        "platforms": ["voice"],
        "max_person_ids": 8,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    app = _app(
        keyring,
        contact_grants=ContactGrantRegistry(tmp_path / "contact-grants.json"),
    )
    monkeypatch.setattr(host, "_contacts_store", Contacts())
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("forged-body-person", "resolved-sender-turn")
    payload["context"]["channel_id"] = "voice:call-guest"
    payload["sender"] = {
        "platform": "voice",
        "user_id": "transport-speaker-7",
        "display_name": "Guest",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["contact_id"] == data["subject_person_id"] == "contact-guest"
    assert data["identity_attested"] is True
    assert data["scope_attested"] is True
    assert data["attribution_method"] == "resolved_sender"
    assert data["source_platform"] == "voice"
    assert data["source_platform_attested"] is True
    assert data["viewer_scope"] == "person:contact-guest"
    assert data["shareability"] == "subject_private"


@pytest.mark.asyncio
async def test_dynamic_sender_grant_retry_keeps_attribution_and_digest_stable(
    tmp_path, graph, monkeypatch,
):
    from types import SimpleNamespace
    from colony_sidecar.events import journal as event_journal

    class Contacts:
        async def resolve_messaging_handle(self, platform, user_id):
            return SimpleNamespace(contact_id="contact-guest")

        async def record_interaction(self, _contact_id):
            return None

        async def get(self, _contact_id):
            return None

    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["attested_contact_grants"] = {
        "platforms": ["voice"], "max_person_ids": 1,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    app = _app(
        keyring,
        contact_grants=ContactGrantRegistry(tmp_path / "contact-grants.json"),
    )
    monkeypatch.setattr(host, "_contacts_store", Contacts())
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("forged", "unused")
    payload["context"].pop("turn_id")
    payload["context"]["channel_id"] = "voice:call-retry"
    payload["sender"] = {
        "platform": "voice", "user_id": "guest-voice", "display_name": "Guest",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"), json=payload,
        )
        retry = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"), json=payload,
        )

    assert first.status_code == retry.status_code == 200
    assert [row[1]["attribution_method"] for row in recorded] == [
        "resolved_sender", "resolved_sender",
    ]
    assert recorded[0][1]["turn_id"] == recorded[1][1]["turn_id"]


@pytest.mark.asyncio
async def test_dynamic_sender_grant_cap_failure_does_not_attest_identity(
    tmp_path, graph, monkeypatch,
):
    from types import SimpleNamespace
    from colony_sidecar.events import journal as event_journal

    class Contacts:
        async def resolve_messaging_handle(self, platform, user_id):
            return SimpleNamespace(contact_id="contact-over-cap")

        async def record_interaction(self, _contact_id):
            return None

        async def get(self, _contact_id):
            return None

    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["attested_contact_grants"] = {
        "platforms": ["voice"], "max_person_ids": 1,
    }
    registry = ContactGrantRegistry(tmp_path / "contact-grants.json")
    assert registry.grant(
        "hermes-text", "contact-existing", max_person_ids=1,
    ) is True
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    app = _app(keyring, contact_grants=registry)
    monkeypatch.setattr(host, "_contacts_store", Contacts())
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("forged", "cap-failure")
    payload["context"]["channel_id"] = "voice:call-cap"
    payload["sender"] = {
        "platform": "voice", "user_id": "over-cap", "display_name": "Guest",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"), json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["contact_id"] == "contact-over-cap"
    assert data["identity_attested"] is False
    assert data["scope_attested"] is False
    assert data["attribution_method"] == "unattested"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["rcs", "operator"])
async def test_structured_sender_platform_cannot_hide_behind_voice_lane(
    tmp_path, graph, monkeypatch, platform,
):
    from types import SimpleNamespace
    from colony_sidecar.events import journal as event_journal
    from colony_sidecar.self_model.event_concerns import project_conversation_turn

    class Contacts:
        async def resolve_messaging_handle(self, _platform, _user_id):
            return SimpleNamespace(contact_id="contact-guest")

        async def record_interaction(self, _contact_id):
            return None

        async def get(self, _contact_id):
            return None

    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["turn_ingress_platforms"] = [platform]
    principal["attested_contact_grants"] = {
        "platforms": [platform], "max_person_ids": 1,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    app = _app(
        keyring,
        contact_grants=ContactGrantRegistry(tmp_path / "contact-grants.json"),
    )
    monkeypatch.setattr(host, "_contacts_store", Contacts())
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    monkeypatch.setenv(
        "COLONY_TURN_CONCERNS_EXCLUDED_PLATFORMS", "rcs,operator",
    )
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("forged", f"{platform}-voice-claim")
    payload["context"]["channel_id"] = "voice:claimed-lane"
    payload["sender"] = {
        "platform": platform, "user_id": "transport-user", "display_name": "Guest",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync", headers=_headers("scoped-secret"), json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["channel_id"] == "voice:claimed-lane"
    assert data["source_platform"] == platform
    projection, reason, _digest = project_conversation_turn({
        "seq": 1,
        "ulid": f"journal-{platform}-claim",
        "type": "conversation.turn",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "data": data,
    })
    assert projection is None
    assert reason == "excluded_source_platform"


@pytest.mark.asyncio
async def test_resolved_sender_in_static_grant_needs_no_dynamic_platform_grant(
    tmp_path, graph, monkeypatch,
):
    from types import SimpleNamespace
    from colony_sidecar.events import journal as event_journal

    class Contacts:
        async def resolve_messaging_handle(self, platform, user_id):
            assert (platform, user_id) == ("voice", "owner-voice-handle")
            return SimpleNamespace(contact_id="contact-owner")

        async def record_interaction(self, _contact_id):
            return None

        async def get(self, _contact_id):
            return None

    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    # No attested_contact_grants: the resolved target is already the exact
    # static viewer/person grant carried by this scoped voice principal.
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    app = _app(keyring)
    monkeypatch.setattr(host, "_contacts_store", Contacts())
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("forged-body-person", "resolved-owner-turn")
    payload["context"]["channel_id"] = "voice"
    payload["sender"] = {
        "platform": "voice",
        "user_id": "owner-voice-handle",
        "display_name": "Owner",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["contact_id"] == data["subject_person_id"] == "contact-owner"
    assert data["identity_attested"] is True
    assert data["scope_attested"] is True
    assert data["attribution_method"] == "resolved_static_grant"
    assert data["viewer_scope"] == "owner"
    assert data["shareability"] == "owner_private"


@pytest.mark.asyncio
async def test_resolved_static_sender_without_resolve_scope_is_not_attested(
    tmp_path, graph, monkeypatch,
):
    from types import SimpleNamespace
    from colony_sidecar.events import journal as event_journal

    class Contacts:
        async def resolve_messaging_handle(self, platform, user_id):
            return SimpleNamespace(contact_id="contact-owner")

        async def record_interaction(self, _contact_id):
            return None

        async def get(self, _contact_id):
            return None

    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["turns:write"])])
    app = _app(keyring)
    monkeypatch.setattr(host, "_contacts_store", Contacts())
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("contact-owner", "resolved-without-scope")
    payload["context"]["channel_id"] = "voice"
    payload["sender"] = {
        "platform": "voice",
        "user_id": "owner-voice-handle",
        "display_name": "Owner",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["contact_id"] == "contact-owner"
    assert data["identity_attested"] is False
    assert data["scope_attested"] is False
    assert data["attribution_method"] == "unattested"


@pytest.mark.asyncio
async def test_unresolved_structured_sender_cannot_fall_back_to_viewer_attestation(
    tmp_path, graph, monkeypatch,
):
    from colony_sidecar.events import journal as event_journal

    principal = _principal(scopes=["turns:write", "turns:resolve-sender"])
    principal["attested_contact_grants"] = {
        "platforms": ["voice"],
        "max_person_ids": 8,
    }
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [principal])
    app = _app(keyring)
    # The graph fixture deliberately leaves the contacts resolver detached.
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("forged-body-person", "unresolved-sender-turn")
    payload["context"]["channel_id"] = "voice:call-unresolved"
    payload["sender"] = {
        "platform": "voice",
        "user_id": "unknown-speaker",
        "display_name": "Unknown",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["contact_id"] == "contact-owner"
    assert data["identity_attested"] is False
    assert data["scope_attested"] is False
    assert data["attribution_method"] == "unattested"


@pytest.mark.asyncio
async def test_legacy_body_claim_cannot_mint_turn_concern_attestation(
    tmp_path, graph, monkeypatch,
):
    from colony_sidecar.events import journal as event_journal

    app = _app(None, legacy_key="legacy-secret")
    monkeypatch.setenv("COLONY_TURN_CONCERNS", "live")
    monkeypatch.setenv("COLONY_TURN_CONCERNS_CHANNELS", "voice")
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("contact-owner", "legacy-forged-turn")
    payload["context"]["channel_id"] = "voice:forged"
    payload["context"]["metadata"] = {
        "identity_attested": True,
        "scope_attested": True,
        "subject_person_id": "contact-owner",
        "viewer_scope": "owner",
        "shareability": "owner_private",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("legacy-secret"),
            json=payload,
        )

    assert response.status_code == 200
    data = recorded[0][1]
    assert data["identity_attested"] is False
    assert data["scope_attested"] is False
    assert data["attribution_method"] == "unattested"
    assert data["viewer_scope"] == ""
    assert data["shareability"] == ""


@pytest.mark.asyncio
async def test_turn_concern_flag_off_keeps_legacy_journal_shape_exact(
    tmp_path, graph, monkeypatch,
):
    from colony_sidecar.events import journal as event_journal

    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, [_principal(scopes=["turns:write"])])
    app = _app(keyring)
    monkeypatch.delenv("COLONY_TURN_CONCERNS", raising=False)
    recorded = []
    monkeypatch.setattr(
        event_journal,
        "append_event",
        lambda event_type, data: recorded.append((event_type, dict(data))) or 1,
    )
    payload = _turn_payload("contact-owner", "flag-off-turn")
    payload["context"]["channel_id"] = "voice:call-off"
    payload["context"]["metadata"] = {"identity_attested": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/turns/sync",
            headers=_headers("scoped-secret"),
            json=payload,
        )

    assert response.status_code == 200
    assert set(recorded[0][1]) == {
        "contact_id", "session_id", "channel_id", "summary", "topics",
        "tools_used",
    }
