"""Unit tests for ChannelRegistry."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from colony_sidecar.delivery.channels import Channel, ChannelRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove COLONY_CHANNEL_* env vars before each test."""
    for key in list(os.environ.keys()):
        if key.startswith("COLONY_CHANNEL_") or key.endswith("_HOME_CHANNEL"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def temp_json_path():
    """Return a temp path for channels.json, cleaned up after test."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
def mock_contacts_store():
    """Return a mock contact store with one handle."""
    store = MagicMock()
    handle = MagicMock()
    handle.gateway = "imessage"
    handle.address = "+15551234567"
    store.get_handles.return_value = [handle]
    return store


# ── Helpers ───────────────────────────────────────────────────────────────


@pytest.fixture
def write_json(temp_json_path):
    def _write(data):
        Path(temp_json_path).write_text(json.dumps(data))
    return _write


# ── Tests ─────────────────────────────────────────────────────────────────


class TestChannelRegistryLoad:
    """Test loading from all sources."""

    def test_empty_registry(self, temp_json_path):
        registry = ChannelRegistry.load(json_path=temp_json_path)
        assert registry.resolve("owner", "dm") is None
        assert registry.resolve("owner", "home") is None

    def test_env_dm_channel(self, temp_json_path, monkeypatch):
        monkeypatch.setenv("COLONY_CHANNEL_DM_owner", "telegram:@owner")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("owner", "dm")
        assert ch is not None
        assert ch.platform == "telegram"
        assert ch.chat_id == "@owner"
        assert ch.channel_type == "dm"

    def test_env_home_channel(self, temp_json_path, monkeypatch):
        monkeypatch.setenv("COLONY_CHANNEL_HOME", "discord:#general")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("__global__", "home")
        assert ch is not None
        assert ch.platform == "discord"
        assert ch.chat_id == "#general"

    def test_env_home_per_person(self, temp_json_path, monkeypatch):
        monkeypatch.setenv("COLONY_CHANNEL_HOME_owner", "signal:+15551234567")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("owner", "home")
        assert ch is not None
        assert ch.platform == "signal"
        assert ch.chat_id == "+15551234567"

    def test_json_contacts(self, temp_json_path, write_json):
        write_json({
            "contacts": {
                "owner": {
                    "dm": {"platform": "telegram", "chat_id": "@username"},
                    "home": {"platform": "discord", "chat_id": "#general"},
                }
            }
        })
        registry = ChannelRegistry.load(json_path=temp_json_path)
        dm = registry.resolve("owner", "dm")
        home = registry.resolve("owner", "home")
        assert dm.platform == "telegram"
        assert home.platform == "discord"

    def test_json_fallback(self, temp_json_path, write_json):
        write_json({
            "fallback": {
                "home": {"platform": "telegram", "chat_id": "@groupname"},
            }
        })
        registry = ChannelRegistry.load(json_path=temp_json_path)
        home = registry.resolve("unknown_person", "home")
        assert home.platform == "telegram"

    def test_priority_env_over_json(self, temp_json_path, write_json, monkeypatch):
        monkeypatch.setenv("COLONY_CHANNEL_DM_owner", "signal:+1555ZZZZZZZ")
        write_json({
            "contacts": {
                "owner": {
                    "dm": {"platform": "telegram", "chat_id": "@username"},
                }
            }
        })
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("owner", "dm")
        assert ch.platform == "signal"  # env wins

    def test_home_channel_env_fallback(self, temp_json_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "@groupname")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("owner", "home")
        assert ch is not None
        assert ch.platform == "telegram"
        assert ch.chat_id == "@groupname"

    def test_home_channel_whatsapp_fallback(self, temp_json_path, monkeypatch):
        monkeypatch.setenv("WHATSAPP_HOME_CHANNEL", "GROUP_ID@g.us")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("owner", "home")
        assert ch is not None
        assert ch.platform == "whatsapp"

    def test_handle_inference(self, temp_json_path, mock_contacts_store):
        registry = ChannelRegistry.load(
            json_path=temp_json_path,
            handle_inference=True,
            contacts_store=mock_contacts_store,
        )
        ch = registry.resolve("owner", "dm")
        assert ch is not None
        assert ch.platform == "whatsapp"  # imessage → whatsapp default mapping
        assert ch.chat_id == "+15551234567"

    def test_handle_inference_disabled(self, temp_json_path, mock_contacts_store):
        registry = ChannelRegistry.load(
            json_path=temp_json_path,
            handle_inference=False,
            contacts_store=mock_contacts_store,
        )
        ch = registry.resolve("owner", "dm")
        assert ch is None

    def test_custom_gateway_map(self, temp_json_path, mock_contacts_store, monkeypatch):
        monkeypatch.setenv("COLONY_CHANNEL_GATEWAY_MAP", '{"imessage": "telegram"}')
        registry = ChannelRegistry.load(
            json_path=temp_json_path,
            handle_inference=True,
            contacts_store=mock_contacts_store,
        )
        ch = registry.resolve("owner", "dm")
        assert ch.platform == "telegram"

    def test_phone_normalization(self, temp_json_path):
        store = MagicMock()
        handle = MagicMock()
        handle.gateway = "imessage"
        handle.address = "(555) 123-4567"
        store.get_handles.return_value = [handle]
        registry = ChannelRegistry.load(
            json_path=temp_json_path,
            handle_inference=True,
            contacts_store=store,
        )
        ch = registry.resolve("owner", "dm")
        assert ch.chat_id == "+5551234567"

    def test_owner_contact_id_alias(self, temp_json_path, monkeypatch):
        """COLONY_CHANNEL_DM_owner resolves for the owner's contact UUID."""
        monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-test-12345-owner")
        monkeypatch.setenv("COLONY_CHANNEL_DM_owner", "whatsapp:+1555ZZZZZZZ")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        # Should resolve by UUID
        ch = registry.resolve("cid-test-12345-owner", "dm")
        assert ch is not None
        assert ch.platform == "whatsapp"
        assert ch.chat_id == "+1555ZZZZZZZ"
        # Should also resolve by "owner" alias directly
        ch2 = registry.resolve("owner", "dm")
        assert ch2 == ch

    def test_async_get_handles_skipped_in_sync_resolve(self, temp_json_path):
        """Async get_handles is never called from sync resolve — no crash, no warning."""

        class AsyncStore:
            async def get_handles(self, contact_id):
                return []

        store = AsyncStore()
        registry = ChannelRegistry.load(
            json_path=temp_json_path,
            handle_inference=True,
            contacts_store=store,
        )
        # Must not crash — returns None gracefully
        ch = registry.resolve("owner", "dm")
        assert ch is None

    def test_system_initiative_no_dm(self, temp_json_path):
        """System initiatives (no person_id) should not try DM."""
        registry = ChannelRegistry.load(json_path=temp_json_path)
        ch = registry.resolve("__system__", "dm")
        assert ch is None


class TestChannelRegistryReload:
    """Test the reload() method."""

    def test_reload_picks_up_new_env(self, temp_json_path, monkeypatch):
        monkeypatch.setenv("COLONY_CHANNEL_DM_owner", "telegram:@old")
        registry = ChannelRegistry.load(json_path=temp_json_path)
        assert registry.resolve("owner", "dm").chat_id == "@old"

        monkeypatch.setenv("COLONY_CHANNEL_DM_owner", "telegram:@new")
        registry.reload()
        assert registry.resolve("owner", "dm").chat_id == "@new"


class TestChannelDataclass:
    """Test the Channel dataclass."""

    def test_channel_creation(self):
        ch = Channel(platform="whatsapp", chat_id="+1555XXXXXXX", channel_type="dm")
        assert ch.platform == "whatsapp"
        assert ch.chat_id == "+1555XXXXXXX"
        assert ch.channel_type == "dm"

    def test_channel_equality(self):
        a = Channel(platform="telegram", chat_id="@user", channel_type="dm")
        b = Channel(platform="telegram", chat_id="@user", channel_type="dm")
        assert a == b

    def test_channel_inequality(self):
        a = Channel(platform="telegram", chat_id="@user", channel_type="dm")
        b = Channel(platform="whatsapp", chat_id="@user", channel_type="dm")
        assert a != b
