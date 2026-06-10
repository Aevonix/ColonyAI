"""Contact store persistence + env config (v0.17.0).

The production server must not run the contact store in :memory: — the
IdentityResolver treats it as the source of truth for the owner, so it
has to survive restarts.
"""

import os

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore


def test_from_env_defaults_to_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("COLONY_CONTACTS_DB", raising=False)
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    cfg = ContactsConfig.from_env()
    assert cfg.sqlite_path == os.path.join(str(tmp_path), "colony-contacts.db")


def test_from_env_explicit_path_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_CONTACTS_DB", str(tmp_path / "custom.db"))
    assert ContactsConfig.from_env().sqlite_path == str(tmp_path / "custom.db")


async def test_contacts_survive_reconnect(tmp_path):
    path = str(tmp_path / "contacts.db")

    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=path))
    await store.connect()
    contact = await store.create(display_name="Owner Test", trust_tier="inner_circle")
    await store.add_handle(contact.contact_id, gateway="whatsapp",
                           address="12345@lid", is_primary=True)
    await store.close()

    # Fresh connection on the same file — the restart scenario.
    store2 = SQLiteContactStore(config=ContactsConfig(sqlite_path=path))
    await store2.connect()
    loaded = await store2.get(contact.contact_id)
    assert loaded is not None
    assert loaded.display_name == "Owner Test"
    by_handle = await store2.resolve_handle("whatsapp", "12345@lid")
    assert by_handle is not None and by_handle.contact_id == contact.contact_id
    await store2.close()
