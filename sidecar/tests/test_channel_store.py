"""Tests for the channel registration store and API router."""

import pytest

from colony_sidecar.channels.manifest import ChannelManifest
from colony_sidecar.channels.store import ChannelStore


@pytest.fixture
def store():
    s = ChannelStore(":memory:")
    s.connect()
    yield s
    s.close()


def _manifest(key="test-channel", **overrides):
    defaults = dict(
        channel_key=key,
        display_name="Test Channel",
        gateway_family="messaging",
    )
    defaults.update(overrides)
    return ChannelManifest(**defaults)


# ── Registration ─────────────────────────────────────────────────────────


class TestRegistration:
    def test_register_new_channel(self, store):
        m = _manifest()
        registered = store.register(m)
        assert registered.channel_key == "test-channel"
        assert registered.status == "active"
        assert registered.channel_token.startswith("ch_")

    def test_register_returns_raw_token_only_on_creation(self, store):
        m = _manifest()
        registered = store.register(m)
        raw_token = registered.channel_token
        assert raw_token.startswith("ch_")
        # Fetching again returns the hash, not the raw token
        fetched = store.get("test-channel")
        assert fetched is not None
        assert not fetched.channel_token.startswith("ch_")

    def test_register_duplicate_without_token_raises(self, store):
        store.register(_manifest())
        with pytest.raises(ValueError, match="already registered"):
            store.register(_manifest())

    def test_register_duplicate_with_correct_token_updates(self, store):
        registered = store.register(_manifest())
        token = registered.channel_token
        updated_manifest = _manifest(display_name="Updated Name")
        updated = store.register(updated_manifest, channel_token=token)
        assert updated.display_name == "Updated Name"

    def test_register_duplicate_with_wrong_token_raises(self, store):
        store.register(_manifest())
        with pytest.raises(ValueError, match="already registered"):
            store.register(_manifest(), channel_token="ch_wrong_token")

    def test_manifest_round_trips(self, store):
        m = _manifest(
            supports_media=True,
            supports_reactions=True,
            max_message_length=4096,
            phone_identity_unification=True,
            delivery_aliases=["sms", "imessage"],
            home_chat_id="group123@g.us",
        )
        store.register(m)
        fetched = store.get("test-channel")
        assert fetched is not None
        assert fetched.manifest.supports_media is True
        assert fetched.manifest.max_message_length == 4096
        assert fetched.manifest.phone_identity_unification is True
        assert fetched.manifest.delivery_aliases == ["sms", "imessage"]
        assert fetched.manifest.home_chat_id == "group123@g.us"


# ── Queries ──────────────────────────────────────────────────────────────


class TestQueries:
    def test_get_nonexistent(self, store):
        assert store.get("nope") is None

    def test_list_active(self, store):
        store.register(_manifest("a"))
        store.register(_manifest("b"))
        store.register(_manifest("c"))
        store.revoke("b")
        active = store.list_active()
        keys = [ch.channel_key for ch in active]
        assert keys == ["a", "c"]

    def test_list_all(self, store):
        store.register(_manifest("a"))
        store.register(_manifest("b"))
        store.revoke("b")
        all_ch = store.list_all()
        assert len(all_ch) == 2

    def test_get_phone_gateways(self, store):
        store.register(_manifest("sms", phone_identity_unification=True))
        store.register(_manifest("whatsapp", phone_identity_unification=True))
        store.register(_manifest("telegram", phone_identity_unification=False))
        gateways = store.get_phone_gateways()
        assert gateways == {"sms", "whatsapp"}


# ── Mutations ────────────────────────────────────────────────────────────


class TestMutations:
    def test_touch_updates_last_seen(self, store):
        registered = store.register(_manifest())
        original_last_seen = store.get("test-channel").last_seen_at
        store.touch("test-channel")
        updated = store.get("test-channel")
        assert updated.last_seen_at >= original_last_seen

    def test_revoke(self, store):
        store.register(_manifest())
        assert store.revoke("test-channel") is True
        ch = store.get("test-channel")
        assert ch.status == "revoked"

    def test_revoke_nonexistent(self, store):
        assert store.revoke("nope") is False

    def test_delete(self, store):
        store.register(_manifest())
        assert store.delete("test-channel") is True
        assert store.get("test-channel") is None


# ── Auth ─────────────────────────────────────────────────────────────────


class TestAuth:
    def test_verify_correct_token(self, store):
        registered = store.register(_manifest())
        assert store.verify_token("test-channel", registered.channel_token) is True

    def test_verify_wrong_token(self, store):
        store.register(_manifest())
        assert store.verify_token("test-channel", "ch_wrong") is False

    def test_verify_nonexistent_channel(self, store):
        assert store.verify_token("nope", "ch_whatever") is False


# ── Migration runner integration ─────────────────────────────────────────


class TestMigrationIntegration:
    def test_schema_version_tracked(self, store):
        from colony_sidecar.migrations import applied_versions_sync
        versions = applied_versions_sync(store._conn)
        assert "001" in versions
