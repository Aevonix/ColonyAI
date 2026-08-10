"""Transactional owner-operated contact provisioning."""

from __future__ import annotations

import json

import pytest

from colony_sidecar.channels.phone_gateways import set_channel_store_ref
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore


async def _store(tmp_path) -> SQLiteContactStore:
    value = SQLiteContactStore(
        ContactsConfig(sqlite_path=str(tmp_path / "contacts.db"))
    )
    await value.connect()
    return value


async def _table_counts(store: SQLiteContactStore) -> dict[str, int]:
    db = store._require_db()
    counts = {}
    for table in (
        "contacts",
        "contact_handles",
        "contact_audit",
        "contact_provision_operations",
    ):
        async with db.execute(
            f"SELECT COUNT(*) AS count FROM {table}"
        ) as cur:
            counts[table] = int((await cur.fetchone())["count"])
    return counts


@pytest.mark.asyncio
async def test_create_is_atomic_inert_verified_audited_and_durably_idempotent(
    tmp_path,
):
    store = await _store(tmp_path)
    try:
        first = await store.provision_verified_handle(
            operation_id="deck-provision-create-0001",
            performed_by="host-operator-contact-authority",
            gateway="whatsapp",
            address="12125550199@s.whatsapp.net",
            display_name="Approved Contact",
        )
        repeated = await store.provision_verified_handle(
            operation_id="deck-provision-create-0001",
            performed_by="host-operator-contact-authority",
            gateway="whatsapp",
            address="12125550199@s.whatsapp.net",
            display_name="Approved Contact",
        )

        assert repeated == first
        assert first["created"] is True
        assert first["handle_created"] is True
        assert first["verified"] is True
        assert first["interaction_allowed"] is False
        contact = await store.get(first["contact_id"])
        assert contact is not None
        assert contact.display_name == "Approved Contact"
        assert contact.interaction_allowed is False
        handles = await store.get_handles(first["contact_id"])
        assert len(handles) == 1
        assert handles[0].address == "12125550199@s.whatsapp.net"
        assert handles[0].verified is True
        audits = await store.get_audit_log(first["contact_id"])
        verified = next(
            row for row in audits
            if row["action"] == "contact_handle_owner_verified"
        )
        detail = json.loads(verified["detail"])
        assert detail["operation_id"] == "deck-provision-create-0001"
        assert detail["address_sha256"]
        assert "12125550199" not in verified["detail"]
        assert verified["performed_by"] == "host-operator-contact-authority"
        assert len(await store.list(limit=100)) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_map_exact_existing_handle_owner_verifies_without_changing_standing(
    tmp_path,
):
    store = await _store(tmp_path)
    try:
        contact = await store.create(
            display_name="Existing Contact", interaction_allowed=False
        )
        original = await store.add_handle(
            contact.contact_id,
            "whatsapp",
            "12125550200@s.whatsapp.net",
            verified=False,
        )
        result = await store.provision_verified_handle(
            operation_id="deck-provision-verify-0001",
            performed_by="host-operator-contact-authority",
            gateway="whatsapp",
            address="12125550200@s.whatsapp.net",
            contact_id=contact.contact_id,
        )
        handles = await store.get_handles(contact.contact_id)
        assert result["created"] is False
        assert result["handle_created"] is False
        assert result["handle_id"] == original.handle_id
        assert result["changed"] is True
        assert result["interaction_allowed"] is False
        assert len(handles) == 1
        assert handles[0].verified is True
        assert (await store.get(contact.contact_id)).interaction_allowed is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_map_can_attach_exact_handle_but_never_move_one_between_contacts(
    tmp_path,
):
    store = await _store(tmp_path)
    try:
        first = await store.create(display_name="First", interaction_allowed=False)
        second = await store.create(display_name="Second", interaction_allowed=False)
        await store.add_handle(
            first.contact_id,
            "whatsapp",
            "12125550201@s.whatsapp.net",
            verified=True,
        )
        with pytest.raises(ValueError, match="another contact"):
            await store.provision_verified_handle(
                operation_id="deck-provision-conflict-0001",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550201@s.whatsapp.net",
                contact_id=second.contact_id,
            )
        assert await store.get_handles(second.contact_id) == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("gateway", "stored_address"), [
    ("sms", "+1 (212) 555-0200"),
    ("rcs", "1-212-555-0200"),
    ("imessage", "(212) 555-0200"),
    ("signal", "1 212 555 0200"),
    ("whatsapp", "+1 (212) 555-0200"),
])
async def test_create_rejects_phone_equivalent_owner_handle_without_orphans(
    tmp_path,
    gateway,
    stored_address,
):
    store = await _store(tmp_path)
    try:
        owner = await store.create(
            display_name="Owner", interaction_allowed=True
        )
        await store.add_handle(
            owner.contact_id,
            gateway,
            stored_address,
            verified=True,
        )
        before = await _table_counts(store)

        with pytest.raises(ValueError, match="phone-equivalent"):
            await store.provision_verified_handle(
                operation_id=f"deck-provision-owner-{gateway}",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550200@s.whatsapp.net",
                display_name="Must Not Shadow Owner",
            )

        assert await _table_counts(store) == before
        assert [item.contact_id for item in await store.list(limit=100)] == [
            owner.contact_id
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_rejects_configured_phone_gateway_equivalent(tmp_path):
    class _ConfiguredChannels:
        def get_phone_gateways(self):
            return {"custom-phone"}

    set_channel_store_ref(_ConfiguredChannels())
    store = await _store(tmp_path)
    try:
        existing = await store.create(
            display_name="Configured phone contact", interaction_allowed=False
        )
        await store.add_handle(
            existing.contact_id,
            "custom-phone",
            "+1 212 555 0201",
            verified=True,
        )
        before = await _table_counts(store)

        with pytest.raises(ValueError, match="phone-equivalent"):
            await store.provision_verified_handle(
                operation_id="deck-provision-custom-phone-conflict",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550201@s.whatsapp.net",
                display_name="Must Not Duplicate Configured Phone",
            )

        assert await _table_counts(store) == before
    finally:
        await store.close()
        set_channel_store_ref(None)


@pytest.mark.asyncio
async def test_map_rejects_phone_equivalent_owned_by_another_contact(tmp_path):
    store = await _store(tmp_path)
    try:
        first = await store.create(
            display_name="First phone identity", interaction_allowed=False
        )
        second = await store.create(
            display_name="Second contact", interaction_allowed=False
        )
        await store.add_handle(
            first.contact_id,
            "signal",
            "+1 (212) 555-0202",
            verified=True,
        )
        before = await _table_counts(store)

        with pytest.raises(ValueError, match="another contact"):
            await store.provision_verified_handle(
                operation_id="deck-provision-cross-contact-phone",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550202@s.whatsapp.net",
                contact_id=second.contact_id,
            )

        assert await _table_counts(store) == before
        assert await store.get_handles(second.contact_id) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_map_can_add_same_contact_phone_equivalent_and_retry(tmp_path):
    store = await _store(tmp_path)
    try:
        contact = await store.create(
            display_name="One phone identity", interaction_allowed=False
        )
        await store.add_handle(
            contact.contact_id,
            "sms",
            "+1 (212) 555-0203",
            verified=True,
        )
        arguments = {
            "operation_id": "deck-provision-same-contact-phone",
            "performed_by": "host-operator-contact-authority",
            "gateway": "whatsapp",
            "address": "12125550203@s.whatsapp.net",
            "contact_id": contact.contact_id,
        }

        first = await store.provision_verified_handle(**arguments)
        repeated = await store.provision_verified_handle(**arguments)

        assert repeated == first
        assert first["created"] is False
        assert first["handle_created"] is True
        assert first["verified"] is True
        handles = await store.get_handles(contact.contact_id)
        assert {(item.gateway, item.address) for item in handles} == {
            ("sms", "+12125550203"),
            ("whatsapp", "12125550203@s.whatsapp.net"),
        }
        resolved = await store.resolve_messaging_handle(
            "whatsapp", "12125550203@s.whatsapp.net"
        )
        assert resolved is not None
        assert resolved.contact_id == contact.contact_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_rejects_ambiguous_name_and_handle_without_orphan_contact(
    tmp_path,
):
    store = await _store(tmp_path)
    try:
        existing = await store.create(
            display_name="Same Name", interaction_allowed=False
        )
        await store.add_handle(
            existing.contact_id,
            "whatsapp",
            "12125550202@s.whatsapp.net",
            verified=True,
        )
        with pytest.raises(ValueError, match="display_name is not unique"):
            await store.provision_verified_handle(
                operation_id="deck-provision-name-conflict",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550203@s.whatsapp.net",
                display_name="same name",
            )
        with pytest.raises(ValueError, match="already assigned"):
            await store.provision_verified_handle(
                operation_id="deck-provision-handle-conflict",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550202@s.whatsapp.net",
                display_name="Different Name",
            )
        assert [item.contact_id for item in await store.list(limit=100)] == [
            existing.contact_id
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_operation_id_cannot_be_rebound_to_body_or_principal(tmp_path):
    store = await _store(tmp_path)
    try:
        await store.provision_verified_handle(
            operation_id="deck-provision-bound-0001",
            performed_by="host-operator-contact-authority",
            gateway="whatsapp",
            address="12125550204@s.whatsapp.net",
            display_name="Bound Contact",
        )
        for changes in (
            {"display_name": "Other Contact"},
            {"performed_by": "other-principal"},
        ):
            arguments = {
                "operation_id": "deck-provision-bound-0001",
                "performed_by": "host-operator-contact-authority",
                "gateway": "whatsapp",
                "address": "12125550204@s.whatsapp.net",
                "display_name": "Bound Contact",
            }
            arguments.update(changes)
            with pytest.raises(ValueError, match="already bound"):
                await store.provision_verified_handle(**arguments)
        assert len(await store.list(limit=100)) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_shared_commit_cannot_persist_failed_provision_transaction(tmp_path):
    store = await _store(tmp_path)
    try:
        db = store._require_db()
        await db.executescript("""
            CREATE TRIGGER fail_owner_verification_audit
            BEFORE INSERT ON contact_audit
            WHEN NEW.action = 'contact_handle_owner_verified'
            BEGIN
              SELECT RAISE(ABORT, 'forced audit failure');
            END;
        """)
        await db.commit()
        before = await _table_counts(store)
        shared_commits = 0

        async def interpose_shared_commit():
            nonlocal shared_commits
            shared_commits += 1
            await db.commit()

        store._after_provision_contact_insert = interpose_shared_commit
        with pytest.raises(Exception, match="forced audit failure"):
            await store.provision_verified_handle(
                operation_id="deck-provision-rollback-0001",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550205@s.whatsapp.net",
                display_name="Must Roll Back",
            )
        assert shared_commits == 1
        assert await _table_counts(store) == before
        assert await store.list(limit=100) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_provisioning_fails_closed_for_in_memory_store():
    store = SQLiteContactStore(ContactsConfig(sqlite_path=":memory:"))
    await store.connect()
    try:
        before = await _table_counts(store)
        with pytest.raises(RuntimeError, match="durable file-backed"):
            await store.provision_verified_handle(
                operation_id="deck-provision-memory-rejected",
                performed_by="host-operator-contact-authority",
                gateway="whatsapp",
                address="12125550206@s.whatsapp.net",
                display_name="Cannot Be Durable",
            )
        assert await _table_counts(store) == before
    finally:
        await store.close()
