"""ContactPolicy source stays read-only and scoped to its exact caller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import required_scope
from colony_sidecar.api.contact_grants import ContactGrantRegistry
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host


def _write_private(path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _principal(
    *,
    principal: str,
    secret: str,
    scopes: list[str],
    attested: bool = False,
) -> dict:
    value = {
        "principal": principal,
        "status": "active",
        "scopes": scopes,
        "allow_unscoped_api": False,
        "viewer_person_id": "cid-owner",
        "person_ids": ["cid-owner"],
        "audiences": ["viewer"],
        "credentials": [{"id": principal + "-key", "secret": secret, "status": "active"}],
    }
    if attested:
        value["attested_contact_grants"] = {
            "platforms": ["whatsapp"],
            "max_person_ids": 8,
        }
    return value


def _contact(contact_id: str, *, name: str, allowed: bool, owner: bool = False):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        contact_id=contact_id,
        display_name=name,
        trust_tier="inner_circle" if owner else "regular",
        privacy_level="private",
        interaction_allowed=allowed,
        first_seen_at=(now - timedelta(days=60)).isoformat(),
        last_interaction_at=(now - timedelta(days=2)).isoformat(),
        interaction_count=6,
        notes="must never cross the projection",
    )


def _handle(
    contact_id: str,
    address: str,
    *,
    gateway: str = "whatsapp",
    primary: bool = True,
    verified: bool = True,
):
    return SimpleNamespace(
        contact_id=contact_id,
        gateway=gateway,
        address=address,
        is_primary=primary,
        verified=verified,
    )


class _Contacts:
    def __init__(self, contacts, handles):
        self.contacts = list(contacts)
        self.handles = dict(handles)
        self.reads = []
        self.mutations = []
        self.provision_operations = {}

    async def list(self, **kwargs):
        self.reads.append(("list", dict(kwargs)))
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 100)
        return self.contacts[offset:offset + limit]

    async def get_handles(self, contact_id):
        self.reads.append(("handles", contact_id))
        return list(self.handles.get(contact_id, ()))

    async def get(self, contact_id):
        self.reads.append(("get", contact_id))
        return next(
            (item for item in self.contacts if item.contact_id == contact_id),
            None,
        )

    async def resolve_handle(self, gateway, address):
        self.reads.append(("resolve_handle", gateway, address))
        for contact_id, handles in self.handles.items():
            if any(
                item.gateway == gateway and item.address == address
                for item in handles
            ):
                return await self.get(contact_id)
        return None

    async def find_by_person_node_id(self, person_node_id):
        self.reads.append(("find_by_person_node_id", person_node_id))
        return None

    async def find_by_name(self, name, threshold=0.9):
        self.reads.append(("find_by_name", name, threshold))
        wanted = str(name).strip().lower()
        return [
            item for item in self.contacts
            if str(item.display_name or "").strip().lower() == wanted
        ]

    async def create(self, **kwargs):
        self.mutations.append(("create", kwargs))
        raise AssertionError("contact policy must not create contacts")

    async def add_handle(self, *args, **kwargs):
        self.mutations.append(("add_handle", args, kwargs))
        raise AssertionError("contact policy must not add handles")

    async def provision_verified_handle(self, **kwargs):
        self.mutations.append(("provision_verified_handle", dict(kwargs)))
        operation_id = kwargs["operation_id"]
        fingerprint = json.dumps(kwargs, sort_keys=True)
        prior = self.provision_operations.get(operation_id)
        if prior is not None:
            if prior[0] != fingerprint:
                raise ValueError("operation_id is already bound")
            return dict(prior[1])
        address = kwargs["address"]
        gateway = kwargs["gateway"]
        selected_id = kwargs.get("contact_id")
        display_name = kwargs.get("display_name")
        existing = None
        phone_gateways = {"sms", "rcs", "imessage", "signal", "whatsapp"}

        def phone_key(value):
            digits = "".join(
                character for character in str(value or "")
                if character.isdigit()
            )
            return digits[-10:] if len(digits) >= 10 else digits

        identity_key = phone_key(address) if gateway in phone_gateways else None
        phone_equivalents = []
        for existing_id, handles in self.handles.items():
            for item in handles:
                if item.gateway == gateway and item.address == address:
                    existing = (existing_id, item)
                if (
                    identity_key
                    and item.gateway in phone_gateways
                    and phone_key(item.address) == identity_key
                ):
                    phone_equivalents.append((existing_id, item))
        if selected_id is None:
            if existing is not None:
                raise ValueError("exact handle is already assigned")
            if phone_equivalents:
                raise ValueError("phone-equivalent handle is already assigned")
            if any(
                str(item.display_name or "").casefold()
                == str(display_name or "").casefold()
                for item in self.contacts
            ):
                raise ValueError("display_name is not unique")
            selected_id = "cid-provisioned-0001"
            contact = _contact(
                selected_id, name=display_name, allowed=False
            )
            self.contacts.append(contact)
            handle = _handle(selected_id, address, gateway=gateway)
            self.handles[selected_id] = [handle]
            created = True
            handle_created = True
            changed = True
        else:
            contact = next(
                (item for item in self.contacts if item.contact_id == selected_id),
                None,
            )
            if contact is None:
                raise ValueError("selected contact does not exist")
            if existing is not None and existing[0] != selected_id:
                raise ValueError("exact handle is already assigned to another contact")
            if any(
                existing_id != selected_id
                for existing_id, _item in phone_equivalents
            ):
                raise ValueError(
                    "phone-equivalent handle is already assigned to another contact"
                )
            if existing is None:
                handle = _handle(selected_id, address, gateway=gateway)
                self.handles.setdefault(selected_id, []).append(handle)
                handle_created = True
                changed = True
            else:
                handle = existing[1]
                changed = handle.verified is not True
                handle.verified = True
                handle_created = False
            display_name = contact.display_name
            created = False
        result = {
            "contact_id": selected_id,
            "display_name": display_name,
            "gateway": gateway,
            "address": address,
            "handle_id": "hdl-provisioned-0001",
            "created": created,
            "handle_created": handle_created,
            "changed": changed,
            "verified": True,
            "interaction_allowed": bool(contact.interaction_allowed),
            "operation_id": operation_id,
        }
        self.provision_operations[operation_id] = (fingerprint, dict(result))
        return result

    async def update_interaction_allowed(self, *args, **kwargs):
        self.mutations.append(("update_interaction_allowed", args, kwargs))
        contact_id, allowed = args
        contact = await self.get(contact_id)
        if contact is None:
            raise ValueError("contact not found")
        contact.interaction_allowed = bool(allowed)

    async def record_audit(self, *args, **kwargs):
        self.mutations.append(("record_audit", args, kwargs))


class _Commitments:
    def list(self, **kwargs):
        return {"commitments": []}


class _Comms:
    def last_outbound(self, contact_id):
        return None


def _app(keyring, grants, *, legacy_key=None):
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=legacy_key,
        keyring_path=str(keyring),
        contact_grants=grants,
    )
    app.include_router(host.router)
    return app


def _headers(secret: str, principal: str | None = None):
    value = {"Authorization": "Bearer " + secret}
    if principal:
        value["X-Colony-Principal"] = principal
    return value


@pytest.fixture
def contact_sources(monkeypatch):
    contacts = _Contacts(
        [
            _contact("cid-owner", name="Owner", allowed=True, owner=True),
            _contact("cid-guest", name="Approved guest", allowed=True),
            _contact("cid-other", name="Held contact", allowed=False),
        ],
        {
            "cid-owner": [_handle("cid-owner", "+12125550123")],
            "cid-guest": [_handle("cid-guest", "12125550199@s.whatsapp.net")],
            "cid-other": [_handle("cid-other", "+12125550200")],
        },
    )
    monkeypatch.setattr(host, "_contacts_store", contacts)
    monkeypatch.setattr(host, "_commitment_store", _Commitments())
    monkeypatch.setattr(host, "_comms_log", _Comms())
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner")
    return contacts


@pytest.mark.asyncio
async def test_contact_policy_projects_only_callers_attested_grants(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [
            _principal(
                principal="deck-reader",
                secret="deck-reader-secret",
                scopes=["contacts:policy-read", "turns:resolve-sender"],
                attested=True,
            ),
            _principal(
                principal="other-reader",
                secret="other-reader-secret",
                scopes=["contacts:policy-read", "turns:resolve-sender"],
                attested=True,
            ),
        ],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {
        "version": 1,
        "principals": {
            "deck-reader": {
                "person_ids": ["cid-guest"],
                "updated_at": "2026-07-17T12:00:00+00:00",
            },
            "other-reader": {
                "person_ids": ["cid-other"],
                "updated_at": "2026-07-17T12:01:00+00:00",
            },
        },
    })
    app = _app(keyring, ContactGrantRegistry(grants_path))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
        post = await c.post(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
            json={"interaction_allowed": True},
        )

    assert response.status_code == 200
    assert post.status_code == 403
    assert post.json()["detail"]["code"] == "unscoped_api_denied"
    body = response.json()
    assert body["schema"] == "ColonyContactPolicySourceV1"
    assert body["read_only"] is True
    assert body["execution_authority"] is False
    assert body["caller_principal"] == "deck-reader"
    assert body["caller_contact_grants"] == {
        "available": True,
        "reason": None,
        "count": 1,
        "updated_at": "2026-07-17T12:00:00+00:00",
    }
    rows = {row["contact_id"]: row for row in body["items"]}
    assert rows["cid-owner"]["is_owner"] is True
    assert rows["cid-owner"]["context_class"] == "owner_private"
    assert rows["cid-guest"]["authority"] == "none"
    assert rows["cid-guest"]["context_class"] == "scoped_or_empty"
    assert rows["cid-guest"]["caller_exact_person_grant"] is True
    assert rows["cid-other"]["caller_exact_person_grant"] is False
    assert rows["cid-other"]["outreach"]["decision"] == "deny"
    assert rows["cid-guest"]["handles"] == [{
        "gateway": "whatsapp",
        "address": "12125550199@s.whatsapp.net",
        "is_primary": True,
        "verified": True,
    }]
    serialized = json.dumps(body, sort_keys=True)
    assert "other-reader" not in serialized
    assert "must never cross" not in serialized
    assert all(
        call[0] in {
            "list", "handles", "get", "resolve_handle",
            "find_by_person_node_id", "find_by_name",
        }
        for call in contact_sources.reads
    )
    assert contact_sources.mutations == []


@pytest.mark.asyncio
async def test_denied_standing_normalizes_an_outreach_recommendation(
    tmp_path, contact_sources, monkeypatch,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-reader",
            secret="deck-reader-secret",
            scopes=["contacts:policy-read"],
        )],
    })

    def recommend(*_args, **_kwargs):
        return {
            "should_contact": True,
            "reason": "overdue follow-up",
            "requires_owner_approval": True,
            "suggested_channel": "whatsapp",
            "cooldown_active": False,
        }

    monkeypatch.setattr(
        "colony_sidecar.contacts.comms.evaluate_outreach", recommend
    )
    app = _app(keyring, ContactGrantRegistry(None))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
    assert response.status_code == 200
    held = next(
        item for item in response.json()["items"]
        if item["contact_id"] == "cid-other"
    )
    assert held["interaction_allowed"] is False
    assert held["outreach"]["decision"] == "deny"
    assert held["outreach"]["should_contact"] is False
    assert held["outreach"]["requires_owner_approval"] is False


@pytest.mark.asyncio
async def test_contact_policy_requires_narrow_scope_and_rejects_legacy(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="wrong-scope",
            secret="wrong-scope-secret",
            scopes=["cognition:read"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None), legacy_key="legacy-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        wrong = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("wrong-scope-secret", "wrong-scope"),
        )
        legacy = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("legacy-secret"),
        )
    assert wrong.status_code == 403
    assert wrong.json()["detail"]["code"] == "insufficient_scope"
    assert legacy.status_code == 403
    assert legacy.json()["detail"]["code"] == "scoped_principal_required"
    assert contact_sources.reads == []


@pytest.mark.asyncio
async def test_contact_policy_paginates_without_claiming_completeness(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-reader",
            secret="deck-reader-secret",
            scopes=["contacts:policy-read"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = await c.get(
            "/v1/host/contact-policy?limit=2&offset=0",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
        second = await c.get(
            "/v1/host/contact-policy?limit=2&offset=2",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
    assert first.status_code == second.status_code == 200
    assert first.json()["complete"] is False
    assert first.json()["truncated"] is True
    assert first.json()["next_offset"] == 2
    assert second.json()["complete"] is True
    assert second.json()["truncated"] is False
    assert second.json()["next_offset"] is None
    assert first.json()["caller_contact_grants"]["available"] is False
    assert second.json()["reason"] is None
    assert all(
        row["caller_exact_person_grant"] is None
        for row in first.json()["items"] + second.json()["items"]
    )


@pytest.mark.asyncio
async def test_malformed_canonical_contact_fails_the_page_instead_of_skipping(
    tmp_path, contact_sources,
):
    contact_sources.contacts[1].contact_id = " cid-guest"
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-reader",
            secret="deck-reader-secret",
            scopes=["contacts:policy-read"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(
            "/v1/host/contact-policy?limit=2",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["complete"] is False
    assert response.json()["reason"] == "contact_identity_invalid"
    assert response.json()["items"] == []
    assert response.json()["next_offset"] is None
    assert contact_sources.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_contact_id", ["cid-\x01guest", "c" * 129])
async def test_contact_policy_rejects_identity_text_that_would_be_normalized(
    tmp_path, contact_sources, bad_contact_id,
):
    contact_sources.contacts[1].contact_id = bad_contact_id
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-reader",
            secret="deck-reader-secret",
            scopes=["contacts:policy-read"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(
            "/v1/host/contact-policy?limit=2",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reason"] == "contact_identity_invalid"
    assert response.json()["items"] == []
    assert contact_sources.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("gateway", "what\x01sapp"), ("address", "1" * 513)],
)
async def test_contact_policy_rejects_handle_text_that_would_be_normalized(
    tmp_path, contact_sources, field, value,
):
    setattr(contact_sources.handles["cid-guest"][0], field, value)
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-reader",
            secret="deck-reader-secret",
            scopes=["contacts:policy-read"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(
            "/v1/host/contact-policy?limit=2",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reason"] == "contact_handle_invalid"
    assert response.json()["items"] == []
    assert contact_sources.mutations == []


@pytest.mark.asyncio
async def test_missing_outreach_dependencies_hold_instead_of_inventing_permission(
    tmp_path, contact_sources, monkeypatch,
):
    monkeypatch.setattr(host, "_commitment_store", None)
    monkeypatch.setattr(host, "_comms_log", None)
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-reader",
            secret="deck-reader-secret",
            scopes=["contacts:policy-read", "turns:resolve-sender"],
            attested=True,
        )],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {
        "version": 1,
        "principals": {"deck-reader": {
            "person_ids": ["cid-guest"],
            "updated_at": "2026-07-17T12:00:00+00:00",
        }},
    })
    app = _app(keyring, ContactGrantRegistry(grants_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["complete"] is False
    assert body["reason"] == "outreach_dependencies_unavailable"
    assert all(row["outreach"]["available"] is False for row in body["items"])
    assert all(row["outreach"]["should_contact"] is False for row in body["items"])
    assert {
        row["outreach"]["decision"] for row in body["items"]
    }.issubset({"hold", "deny"})
    assert contact_sources.mutations == []


def test_contact_policy_has_one_exact_read_scope():
    assert required_scope("GET", "/v1/host/contact-policy") == "contacts:policy-read"
    assert required_scope("POST", "/v1/host/contact-policy") == "api:access"
    assert required_scope(
        "POST", "/v1/host/contact-policy/standing"
    ) == "contacts:policy-write"
    assert required_scope(
        "POST", "/v1/host/contact-policy/provision"
    ) == "contacts:policy-write"


@pytest.mark.asyncio
async def test_contact_provision_create_normalizes_e164_is_inert_and_idempotent(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    payload = {
        "mode": "create",
        "display_name": "New Contact",
        "whatsapp_identity": "+12125550210",
        "operation_id": "deck-provision-create-0010",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json=payload,
        )
        repeated = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json=payload,
        )
    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    result = first.json()
    assert result["schema"] == "ColonyContactProvisionResultV1"
    assert result["address"] == "12125550210@s.whatsapp.net"
    assert result["created"] is True
    assert result["verified"] is True
    assert result["interaction_allowed"] is False
    assert result["principal"] == "deck-writer"
    calls = [
        row for row in contact_sources.mutations
        if row[0] == "provision_verified_handle"
    ]
    assert len(calls) == 2
    assert all(
        row[1]["performed_by"] == "deck-writer" for row in calls
    )
    assert not any(
        row[0] == "update_interaction_allowed"
        for row in contact_sources.mutations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("gateway", "stored_address"), [
    ("sms", "+1 (212) 555-0207"),
    ("rcs", "1-212-555-0207"),
    ("imessage", "(212) 555-0207"),
    ("signal", "1 212 555 0207"),
    ("whatsapp", "+1 (212) 555-0207"),
])
async def test_contact_provision_api_rejects_owner_phone_alias_without_mutation(
    tmp_path,
    contact_sources,
    gateway,
    stored_address,
):
    contact_sources.handles["cid-owner"] = [_handle(
        "cid-owner", stored_address, gateway=gateway
    )]
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    before_contacts = list(contact_sources.contacts)
    before_handles = {
        contact_id: list(handles)
        for contact_id, handles in contact_sources.handles.items()
    }
    app = _app(keyring, ContactGrantRegistry(None))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "mode": "create",
                "display_name": f"Owner shadow via {gateway}",
                "whatsapp_identity": "12125550207@s.whatsapp.net",
                "operation_id": f"deck-provision-owner-alias-{gateway}",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "contact_handle_conflict"
    assert contact_sources.contacts == before_contacts
    assert contact_sources.handles == before_handles
    assert contact_sources.provision_operations == {}


@pytest.mark.asyncio
async def test_contact_provision_maps_selected_contact_and_verifies_exact_handle(
    tmp_path, contact_sources,
):
    contact_sources.handles["cid-other"] = [_handle(
        "cid-other", "12125550200@s.whatsapp.net", verified=False
    )]
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "mode": "verify",
                "contact_id": "cid-other",
                "whatsapp_identity": "12125550200@s.whatsapp.net",
                "operation_id": "deck-provision-verify-0010",
            },
        )
    assert response.status_code == 200
    assert response.json()["contact_id"] == "cid-other"
    assert response.json()["created"] is False
    assert response.json()["handle_created"] is False
    assert response.json()["changed"] is True
    assert response.json()["interaction_allowed"] is False
    assert contact_sources.handles["cid-other"][0].verified is True


@pytest.mark.asyncio
async def test_contact_provision_api_rejects_cross_contact_phone_alias(
    tmp_path, contact_sources,
):
    contact_sources.handles["cid-guest"] = [_handle(
        "cid-guest",
        "+1 (212) 555-0208",
        gateway="signal",
    )]
    contact_sources.handles["cid-other"] = []
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "mode": "verify",
                "contact_id": "cid-other",
                "whatsapp_identity": "12125550208@s.whatsapp.net",
                "operation_id": "deck-provision-api-cross-contact-phone",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "contact_handle_conflict"
    assert contact_sources.handles["cid-other"] == []
    assert contact_sources.provision_operations == {}


@pytest.mark.asyncio
async def test_contact_provision_api_allows_same_contact_phone_alias(
    tmp_path, contact_sources,
):
    contact_sources.handles["cid-other"] = [_handle(
        "cid-other",
        "+1 (212) 555-0209",
        gateway="sms",
    )]
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "mode": "verify",
                "contact_id": "cid-other",
                "whatsapp_identity": "12125550209@s.whatsapp.net",
                "operation_id": "deck-provision-api-same-contact-phone",
            },
        )

    assert response.status_code == 200
    assert response.json()["contact_id"] == "cid-other"
    assert response.json()["handle_created"] is True
    assert {
        (item.gateway, item.address)
        for item in contact_sources.handles["cid-other"]
    } == {
        ("sms", "+1 (212) 555-0209"),
        ("whatsapp", "12125550209@s.whatsapp.net"),
    }


@pytest.mark.asyncio
async def test_contact_provision_denies_untrusted_authority_and_body_overrides(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [
            _principal(
                principal="deck-writer",
                secret="deck-writer-secret",
                scopes=["contacts:policy-write"],
            ),
            _principal(
                principal="deck-reader",
                secret="deck-reader-secret",
                scopes=["contacts:policy-read"],
            ),
        ],
    })
    app = _app(
        keyring, ContactGrantRegistry(None), legacy_key="legacy-key"
    )
    payload = {
        "mode": "verify",
        "contact_id": "cid-other",
        "whatsapp_identity": "12125550200@s.whatsapp.net",
        "operation_id": "deck-provision-denial-0010",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wrong_scope = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-reader-secret", "deck-reader"),
            json=payload,
        )
        legacy = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("legacy-key"),
            json=payload,
        )
        guest = await client.post(
            "/v1/host/contact-policy/provision", json=payload
        )
        owner = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={**payload, "contact_id": "cid-owner"},
        )
        create_override = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "mode": "create",
                "display_name": "Override",
                "contact_id": "cid-other",
                "whatsapp_identity": "+12125550211",
                "operation_id": "deck-provision-override-0010",
            },
        )
        verify_override = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={**payload, "display_name": "Injected Name"},
        )
        authority_override = await client.post(
            "/v1/host/contact-policy/provision",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={**payload, "performed_by": "owner"},
        )
    assert wrong_scope.status_code == 403
    assert legacy.status_code == 403
    assert guest.status_code in {401, 403}
    assert owner.status_code == 409
    assert create_override.status_code == 400
    assert verify_override.status_code == 400
    assert authority_override.status_code == 422
    assert contact_sources.mutations == []


@pytest.mark.asyncio
async def test_contact_provision_rejects_aliases_groups_whitespace_and_cross_gateway(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    app = _app(keyring, ContactGrantRegistry(None))
    invalid = [
        " 12125550212@s.whatsapp.net",
        "12125550212",
        "+01234567890",
        "contact alias",
        "*@s.whatsapp.net",
        "12125550212@g.us",
        "12125550212@broadcast",
        "tel:+12125550212",
        "someone@example.com",
        "12125550212@S.WHATSAPP.NET",
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = []
        for index, identity in enumerate(invalid):
            responses.append(await client.post(
                "/v1/host/contact-policy/provision",
                headers=_headers("deck-writer-secret", "deck-writer"),
                json={
                    "mode": "create",
                    "display_name": "Invalid %d" % index,
                    "whatsapp_identity": identity,
                    "operation_id": "deck-provision-invalid-%04d" % index,
                },
            ))
    assert all(response.status_code == 400 for response in responses)
    assert all(
        response.json()["detail"]["code"]
        == "contact_whatsapp_identity_invalid"
        for response in responses
    )
    assert contact_sources.mutations == []


@pytest.mark.asyncio
async def test_contact_policy_is_complete_without_inbound_attestation_registry(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="outreach-reader",
            secret="outreach-reader-secret",
            scopes=["contacts:policy-read"],
        )],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {"version": 1, "principals": {}})
    app = _app(keyring, ContactGrantRegistry(grants_path))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("outreach-reader-secret", "outreach-reader"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["complete"] is True
    assert body["reason"] is None
    assert body["caller_contact_grants"]["available"] is False
    assert all(
        row["caller_exact_person_grant"] is None for row in body["items"]
    )


@pytest.mark.asyncio
async def test_contact_standing_is_exact_scoped_idempotent_and_audited(
    tmp_path, contact_sources,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [
            _principal(
                principal="deck-writer",
                secret="deck-writer-secret",
                scopes=["contacts:policy-write"],
            ),
            _principal(
                principal="deck-reader",
                secret="deck-reader-secret",
                scopes=["contacts:policy-read"],
            ),
        ],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {"version": 1, "principals": {}})
    app = _app(keyring, ContactGrantRegistry(grants_path), legacy_key="legacy-key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": True,
                "operation_id": "deck-standing-00000001",
            },
        )
        repeated = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": True,
                "operation_id": "deck-standing-00000002",
            },
        )
        wrong_scope = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-reader-secret", "deck-reader"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": False,
                "operation_id": "deck-standing-00000003",
            },
        )
        legacy = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("legacy-key"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": False,
                "operation_id": "deck-standing-00000004",
            },
        )
        owner = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-owner",
                "interaction_allowed": False,
                "operation_id": "deck-standing-00000005",
            },
        )
        unknown = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-missing",
                "interaction_allowed": True,
                "operation_id": "deck-standing-00000006",
            },
        )
        extra = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": True,
                "operation_id": "deck-standing-00000007",
                "trust_tier": "inner_circle",
            },
        )

    assert first.status_code == 200
    assert first.json() == {
        "schema": "ColonyContactStandingResultV1",
        "version": 1,
        "contact_id": "cid-other",
        "interaction_allowed": True,
        "changed": True,
        "operation_id": "deck-standing-00000001",
        "principal": "deck-writer",
    }
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert wrong_scope.status_code == 403
    assert legacy.status_code == 403
    assert legacy.json()["detail"]["code"] == (
        "scoped_contact_policy_writer_required"
    )
    assert owner.status_code == 409
    assert unknown.status_code == 404
    assert extra.status_code == 422
    assert contact_sources.mutations == [
        (
            "update_interaction_allowed",
            ("cid-other", True),
            {"performed_by": "deck-writer"},
        ),
        (
            "record_audit",
            (
                "cid-other",
                "contact_policy_standing_command",
                {
                    "operation_id": "deck-standing-00000001",
                    "interaction_allowed": True,
                    "changed": True,
                },
            ),
            {"performed_by": "deck-writer"},
        ),
        (
            "record_audit",
            (
                "cid-other",
                "contact_policy_standing_command",
                {
                    "operation_id": "deck-standing-00000002",
                    "interaction_allowed": True,
                    "changed": False,
                },
            ),
            {"performed_by": "deck-writer"},
        ),
    ]


@pytest.mark.asyncio
async def test_contact_standing_fails_closed_when_owner_identity_is_unavailable(
    tmp_path, contact_sources, monkeypatch,
):
    monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
    monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [_principal(
            principal="deck-writer",
            secret="deck-writer-secret",
            scopes=["contacts:policy-write"],
        )],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {"version": 1, "principals": {}})
    app = _app(keyring, ContactGrantRegistry(grants_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": True,
                "operation_id": "deck-standing-owner-missing",
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "owner_contact_unavailable"
    assert contact_sources.mutations == []


@pytest.mark.asyncio
async def test_projection_and_mutation_share_legacy_and_name_owner_resolution(
    tmp_path, contact_sources, monkeypatch,
):
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [
            _principal(
                principal="deck-reader",
                secret="deck-reader-secret",
                scopes=["contacts:policy-read"],
            ),
            _principal(
                principal="deck-writer",
                secret="deck-writer-secret",
                scopes=["contacts:policy-write"],
            ),
        ],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {"version": 1, "principals": {}})
    app = _app(keyring, ContactGrantRegistry(grants_path))

    monkeypatch.delenv("COLONY_OWNER_CONTACT_ID", raising=False)
    monkeypatch.setenv("COLONY_HOST_CONTACT_ID", "cid-owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        legacy_read = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
        legacy_write = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-owner",
                "interaction_allowed": False,
                "operation_id": "deck-standing-legacy-owner",
            },
        )
        monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "Owner")
        monkeypatch.delenv("COLONY_HOST_CONTACT_ID", raising=False)
        name_read = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
        name_write = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-owner",
                "interaction_allowed": False,
                "operation_id": "deck-standing-name-owner",
            },
        )

    for response in (legacy_read, name_read):
        assert response.status_code == 200
        rows = {row["contact_id"]: row for row in response.json()["items"]}
        assert rows["cid-owner"]["is_owner"] is True
    assert legacy_write.status_code == name_write.status_code == 409
    assert contact_sources.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_value,ambiguous", [
    ("missing owner", False),
    ("Owner", True),
])
async def test_owner_resolution_uncertainty_returns_no_projection_or_mutation(
    tmp_path, contact_sources, monkeypatch, owner_value, ambiguous,
):
    if ambiguous:
        contact_sources.contacts.append(
            _contact("cid-owner-duplicate", name="Owner", allowed=True)
        )
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", owner_value)
    keyring = tmp_path / "keyring.json"
    _write_private(keyring, {
        "version": 1,
        "principals": [
            _principal(
                principal="deck-reader",
                secret="deck-reader-secret",
                scopes=["contacts:policy-read"],
            ),
            _principal(
                principal="deck-writer",
                secret="deck-writer-secret",
                scopes=["contacts:policy-write"],
            ),
        ],
    })
    grants_path = tmp_path / "contact-grants.json"
    _write_private(grants_path, {"version": 1, "principals": {}})
    app = _app(keyring, ContactGrantRegistry(grants_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        projected = await c.get(
            "/v1/host/contact-policy",
            headers=_headers("deck-reader-secret", "deck-reader"),
        )
        mutated = await c.post(
            "/v1/host/contact-policy/standing",
            headers=_headers("deck-writer-secret", "deck-writer"),
            json={
                "contact_id": "cid-other",
                "interaction_allowed": True,
                "operation_id": "deck-standing-owner-uncertain",
            },
        )
    assert projected.status_code == 200
    assert projected.json()["available"] is False
    assert projected.json()["reason"] == "owner_contact_unresolved"
    assert projected.json()["items"] == []
    assert mutated.status_code == 503
    assert mutated.json()["detail"]["code"] == "owner_contact_unavailable"
    assert contact_sources.mutations == []


def test_caller_grant_projection_fails_closed_on_missing_or_malformed_replacement(
    tmp_path,
):
    grants_path = tmp_path / "contact-grants.json"
    registry = ContactGrantRegistry(grants_path)
    missing = registry.principal_projection("deck-reader", max_person_ids=8)
    assert missing == {
        "available": False,
        "reason": "contact_grant_projection_missing",
        "person_ids": [],
        "updated_at": None,
    }

    _write_private(grants_path, {
        "version": 1,
        "principals": {"deck-reader": {
            "person_ids": ["cid-guest"],
            "updated_at": "2026-07-17T12:00:00+00:00",
        }},
    })
    loaded = registry.principal_projection("deck-reader", max_person_ids=8)
    assert loaded["available"] is True
    assert loaded["person_ids"] == ["cid-guest"]

    replacement = tmp_path / "replacement.json"
    replacement.write_text("{not-json", encoding="utf-8")
    replacement.chmod(0o600)
    replacement.replace(grants_path)
    malformed = registry.principal_projection("deck-reader", max_person_ids=8)
    assert malformed == {
        "available": False,
        "reason": "contact_grant_projection_invalid",
        "person_ids": [],
        "updated_at": None,
    }
