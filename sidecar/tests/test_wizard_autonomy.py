"""Current setup prompts, persisted model configuration and owner creation."""

import json

import pytest

from pacomind.contacts.config import ContactsConfig
from pacomind.contacts.store import SQLiteContactStore
from pacomind.setup import (
    _prompt,
    apply_llm_config_fixes,
    build_owner_contact,
    ensure_api_key,
    normalize_llm_base_url,
    write_llm_host_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Scripted-defaults env var must not leak into prompt assertions."""
    monkeypatch.delenv("PACOMIND_INIT_DEFAULTS", raising=False)


# ── _prompt with injectable ask ──────────────────────────────────────────────


def test_prompt_uses_injected_ask():
    assert _prompt("Name", "default", ask=lambda p: "value") == "value"


def test_prompt_falls_back_to_default_on_blank_answer():
    assert _prompt("Name", "default", ask=lambda p: "") == "default"


def test_prompt_non_interactive_ignores_ask():
    assert _prompt("Name", "default", non_interactive=True, ask=lambda p: "value") == "default"


# ── normalize_llm_base_url ───────────────────────────────────────────────────


def test_base_url_appends_v1_for_vllm():
    url, changed = normalize_llm_base_url("http://localhost:8000", "vllm")
    assert url == "http://localhost:8000/v1"
    assert changed is True


def test_base_url_strips_trailing_slash_before_appending():
    url, changed = normalize_llm_base_url("http://localhost:1234/", "openai-compatible")
    assert url == "http://localhost:1234/v1"
    assert changed is True


@pytest.mark.parametrize("existing", [
    "http://localhost:8000/v1",
    "http://localhost:8000/v1/",
])
def test_base_url_already_v1_unchanged(existing):
    url, changed = normalize_llm_base_url(existing, "vllm")
    assert url == existing
    assert changed is False


@pytest.mark.parametrize("provider", ["ollama", "anthropic", ""])
def test_base_url_non_openai_compat_passes_through(provider):
    url, changed = normalize_llm_base_url("http://localhost:11434", provider)
    assert url == "http://localhost:11434"
    assert changed is False


def test_base_url_empty_passes_through():
    assert normalize_llm_base_url("", "vllm") == ("", False)


@pytest.mark.parametrize("provider", ["lmstudio", "custom", "local", "OpenAI"])
def test_base_url_all_openai_compat_providers_normalized(provider):
    url, changed = normalize_llm_base_url("http://10.0.0.5:9000", provider)
    assert url == "http://10.0.0.5:9000/v1"
    assert changed is True


# ── ensure_api_key ───────────────────────────────────────────────────────────


def test_empty_api_key_defaults_to_local_no_key():
    cfg = {"provider": "vllm", "apiKey": ""}
    fixed, changed = ensure_api_key(cfg)
    assert fixed["apiKey"] == "local-no-key"
    assert changed is True
    # Input dict must not be mutated.
    assert cfg["apiKey"] == ""


def test_whitespace_api_key_treated_as_empty():
    fixed, changed = ensure_api_key({"provider": "lmstudio", "apiKey": "   "})
    assert fixed["apiKey"] == "local-no-key"
    assert changed is True


def test_missing_api_key_field_defaults():
    fixed, changed = ensure_api_key({"provider": "openai-compatible"})
    assert fixed["apiKey"] == "local-no-key"
    assert changed is True


def test_real_api_key_preserved():
    fixed, changed = ensure_api_key({"provider": "vllm", "apiKey": "sk-real"})
    assert fixed["apiKey"] == "sk-real"
    assert changed is False


def test_non_openai_compat_provider_may_keep_empty_key():
    fixed, changed = ensure_api_key({"provider": "ollama", "apiKey": ""})
    assert fixed.get("apiKey") == ""
    assert changed is False


# ── apply_llm_config_fixes / write_llm_host_config ───────────────────────────


def test_apply_llm_config_fixes_applies_both_with_notes():
    cfg = {"provider": "vllm", "baseUrl": "http://localhost:8000", "apiKey": ""}
    fixed, notes = apply_llm_config_fixes(cfg)
    assert fixed["baseUrl"] == "http://localhost:8000/v1"
    assert fixed["apiKey"] == "local-no-key"
    assert len(notes) == 2


def test_apply_llm_config_fixes_noop_for_good_config():
    cfg = {"provider": "vllm", "baseUrl": "http://localhost:8000/v1", "apiKey": "k"}
    fixed, notes = apply_llm_config_fixes(cfg)
    assert fixed == cfg
    assert notes == []


def test_write_llm_host_config_persists_fixed_config(tmp_path):
    path = tmp_path / ".pacomind-llm-config.json"
    cfg = {
        "provider": "openai-compatible",
        "baseUrl": "http://localhost:1234",
        "apiKey": "",
        "models": {"small": "qwen2.5"},
    }
    fixed, notes = write_llm_host_config(path, cfg)
    on_disk = json.loads(path.read_text())
    assert on_disk == fixed
    assert on_disk["baseUrl"] == "http://localhost:1234/v1"
    assert on_disk["apiKey"] == "local-no-key"
    assert on_disk["models"] == {"small": "qwen2.5"}
    assert len(notes) == 2


# ── build_owner_contact (tmp-path store) ─────────────────────────────────────


def _store(tmp_path):
    return SQLiteContactStore(
        config=ContactsConfig(sqlite_path=str(tmp_path / "contacts.db"))
    )


@pytest.mark.asyncio
async def test_build_owner_contact_creates_inner_circle_owner(tmp_path):
    store = _store(tmp_path)
    await store.connect()
    try:
        cid = await build_owner_contact(
            store, "Sam",
            [("whatsapp", "555123@lid"), ("email", "Sam@Example.com")],
        )
        contact = await store.get(cid)
        assert contact is not None
        assert contact.display_name == "Sam"
        assert contact.trust_tier == "inner_circle"
        assert contact.interaction_allowed is True
        assert contact.import_source == "wizard"

        handles = await store.get_handles(cid)
        # Email is normalized to lowercase by the store.
        assert {(h.gateway, h.address) for h in handles} == {
            ("whatsapp", "555123@lid"),
            ("email", "sam@example.com"),
        }
        primary = [h for h in handles if h.is_primary]
        assert len(primary) == 1 and primary[0].gateway == "whatsapp"

        resolved = await store.resolve_handle("email", "sam@example.com")
        assert resolved is not None and resolved.contact_id == cid
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_owner_contact_without_handles(tmp_path):
    store = _store(tmp_path)
    await store.connect()
    try:
        cid = await build_owner_contact(store, "Owner", [])
        assert (await store.get(cid)) is not None
        assert await store.get_handles(cid) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_owner_contact_skips_colliding_handles(tmp_path):
    store = _store(tmp_path)
    await store.connect()
    try:
        other = await store.create(display_name="Someone Else")
        await store.add_handle(other.contact_id, gateway="telegram", address="tg-1")

        cid = await build_owner_contact(
            store, "Owner", [("telegram", "tg-1"), ("signal", "+12025550100")]
        )
        # Owner record exists despite the collision; only the free handle landed.
        assert (await store.get(cid)) is not None
        handles = await store.get_handles(cid)
        assert [(h.gateway, h.address) for h in handles] == [("signal", "+12025550100")]
    finally:
        await store.close()
